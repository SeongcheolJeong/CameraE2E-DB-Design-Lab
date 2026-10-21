#!/usr/bin/env python3
"""Recompute one pinned joint LFM phase and compare pyisetcam time integration.

This is an explicit offline diagnostic.  It does not execute a vendor HDR/LFM
capture program, read an existing LFM NPZ, or update a saved platform result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from camera_platform.lens_sensor_hdr_bridge import _prepare_lens_sensor_capture
from camera_platform.lfm_temporal_parity import crosscheck_pwm_temporal_capture
from camera_platform.saved_sensor_lfm import _on_fraction


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lens-revision-id", required=True)
    parser.add_argument("--sensor-identifier", required=True)
    parser.add_argument("--sensor-run-id", required=True)
    parser.add_argument("--root", type=Path, required=True, help="CameraE2E DB root")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--reference-exposure-ns", type=int, required=True)
    parser.add_argument("--checked-exposure-ns", type=int, required=True)
    parser.add_argument("--period-ns", type=int, required=True)
    parser.add_argument("--on-time-ns", type=int, required=True)
    parser.add_argument("--phase-offset-ns", type=int, required=True)
    parser.add_argument("--line-time-ns", type=int, required=True)
    parser.add_argument("--off-to-on-ratio", type=float, required=True)
    parser.add_argument("--on-luminance-cd-m2", type=float, required=True)
    parser.add_argument("--sensor-size", type=int, nargs=2, default=(8, 8))
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--samples-per-exposure", type=int, nargs=2, default=(128, 512))
    parser.add_argument("--maximum-relative-error", type=float, default=0.003)
    parser.add_argument("--accept-approximate-lens-radiometry", action="store_true")
    parser.add_argument("--allow-numerical-negative-photons", action="store_true")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    if not arguments.accept_approximate_lens_radiometry:
        parser.error("Explicit approximate lens-radiometry acceptance is required.")
    if not 0 < arguments.checked_exposure_ns <= arguments.reference_exposure_ns:
        parser.error("Checked exposure must be positive and no longer than reference exposure.")
    if not 0 < arguments.on_time_ns < arguments.period_ns:
        parser.error("PWM ON time must be strictly inside the declared period.")
    if not 0 <= arguments.off_to_on_ratio < 1:
        parser.error("OFF-to-ON ratio must be in [0, 1).")

    prepared = _prepare_lens_sensor_capture(
        arguments.lens_revision_id,
        arguments.sensor_identifier,
        sensor_run_id=arguments.sensor_run_id,
        root=arguments.root,
        workspace=arguments.workspace,
        exposure_s=arguments.reference_exposure_ns / 1e9,
        scene_kind="uniform",
        sensor_size=tuple(arguments.sensor_size),
        luminance_cd_m2=arguments.on_luminance_cd_m2,
        seed=arguments.seed,
        accept_approximate_lens_radiometry=True,
        psf_compute_mode="bounded_streaming",
    )
    sensor = prepared["sensor"].clone()
    sensor.fields["integration_time"] = arguments.checked_exposure_ns / 1e9
    starts = np.arange(arguments.sensor_size[0], dtype=np.float64) * (
        arguments.line_time_ns / 1e9
    )
    fractions = _on_fraction(
        starts,
        arguments.checked_exposure_ns / 1e9,
        arguments.period_ns / 1e9,
        arguments.on_time_ns / arguments.period_ns,
        arguments.phase_offset_ns / 1e9,
    )
    high_mean = np.asarray(prepared["mean_electrons"], dtype=np.float64) * (
        arguments.checked_exposure_ns / arguments.reference_exposure_ns
    )
    expected = high_mean * (
        arguments.off_to_on_ratio
        + (1 - arguments.off_to_on_ratio) * fractions[:, None]
    )
    comparison = crosscheck_pwm_temporal_capture(
        sensor,
        prepared["optical_image"],
        period_ns=arguments.period_ns,
        on_time_ns=arguments.on_time_ns,
        phase_offset_ns=arguments.phase_offset_ns,
        line_time_ns=arguments.line_time_ns,
        off_to_on_ratio=arguments.off_to_on_ratio,
        expected_lfm_mean_electrons=expected,
        seed=arguments.seed,
        samples_per_exposure=arguments.samples_per_exposure,
        maximum_relative_error=arguments.maximum_relative_error,
        allow_numerical_negative_photons=arguments.allow_numerical_negative_photons,
    )
    payload = {
        "schema": "camerae2e_pinned_lfm_temporal_parity_diagnostic_v1",
        "status": comparison["status"],
        "source": {
            "lens_identity": prepared["lens_identity"],
            "sensor_revision": prepared["sensor_revision"],
            "upstream_assets": prepared["upstream_assets"],
            "psf_execution": prepared["psf_execution"],
            "saved_readout": prepared["saved_readout"],
            "original_on_optical_photons_sha256": hashlib.sha256(
                np.ascontiguousarray(prepared["optical_image"].data["photons"])
                .tobytes(order="C")
            ).hexdigest(),
            "recomputed_lfm_analytic_mean_sha256": hashlib.sha256(
                np.ascontiguousarray(expected).tobytes(order="C")
            ).hexdigest(),
            "saved_lfm_artifact_compared": False,
        },
        "comparison": comparison,
        "product_hdr_lfm_decision": "not_established",
    }
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if arguments.output is None:
        print(rendered)
    else:
        destination = arguments.output.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n", encoding="utf-8")
        print(destination)


if __name__ == "__main__":
    main()

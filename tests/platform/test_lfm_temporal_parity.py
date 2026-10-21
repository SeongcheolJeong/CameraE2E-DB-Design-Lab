"""The authored PWM row integral can be challenged by a separate time engine."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform.lfm_temporal_parity import (
    crosscheck_pwm_four_phase_set,
    crosscheck_pwm_temporal_capture,
    load_verified_lfm_artifact,
)
from camera_platform.saved_sensor_lfm import _on_fraction
from pyisetcam import configure_e2e_sensor, sensor_create, sensor_set
from pyisetcam.sensor import sensor_compute_noise_free
from pyisetcam.types import OpticalImage

SOURCE = (
    Path(__file__).resolve().parents[2]
    / "reports/camera_platform/lfm-temporal-parity-20260928/dev28-source-lfm"
)
SOURCE_REPORT_SHA = "ff1b7f6d6513d7b949cfd417755f915cd07e6bfd55c58d5c253fe6d5f571ccc6"


@pytest.fixture
def optical_sensor():
    sensor = sensor_create("monochrome", {"size": (8, 8), "wave": np.array([550.0])})
    sensor = sensor_set(sensor, "integration time", 0.001)
    sensor = sensor_set(sensor, "noise flag", 2)
    sensor = configure_e2e_sensor(
        sensor,
        readout={"dark_current_e_per_s": 0, "read_noise_e": 0, "full_well_e": 10000},
    )
    optical = OpticalImage(
        fields={
            "wave": np.array([550.0]),
            "width_m": 8 * sensor.fields["pixel"]["size_m"][1],
        },
        data={"photons": np.full((8, 8, 1), 1e15)},
    )
    return sensor, optical


def _source_mean(sensor, optical):
    sampled = sensor_compute_noise_free(sensor, optical)
    return (
        np.asarray(sampled.data["volts"])
        / sampled.fields["pixel"]["conversion_gain_v_per_electron"]
    )


def _compare(sensor, optical, **overrides):
    arguments = {
        "period_ns": 2_000_000,
        "on_time_ns": 1_000_000,
        "phase_offset_ns": 500_000,
        "line_time_ns": 20_000,
        "off_to_on_ratio": 0.02,
    }
    arguments.update(overrides)
    return crosscheck_pwm_temporal_capture(sensor, optical, **arguments)


def test_matches_existing_lfm_analytic_mean_and_converges_through_sensor_engine(
    optical_sensor,
):
    sensor, optical = optical_sensor
    high = _source_mean(sensor, optical)
    fractions = _on_fraction(
        np.arange(8) * 20e-6, 0.001, 0.002, 0.5, 0.0005
    )
    original_lfm_mean = high * (0.02 + 0.98 * fractions[:, None])
    original_photons = optical.data["photons"].copy()
    result = _compare(
        sensor, optical, expected_lfm_mean_electrons=original_lfm_mean
    )

    assert result["status"] == "passed_at_declared_numerical_resolution"
    assert result["saved_lfm_mean_relative_error"] < 1e-7
    assert result["off_state_linearity_relative_error"] < 1e-7
    assert result["comparisons"][1]["maximum_relative_error_to_on_signal"] < (
        result["comparisons"][0]["maximum_relative_error_to_on_signal"] / 2
    )
    assert all(item["readout_matches_selected_sensor"] for item in result["comparisons"])
    assert result["timing"]["row_on_fraction_exact_integer_time"] == pytest.approx(
        fractions.tolist(), abs=1e-12
    )
    assert result["physical_validation"] == "not_established"
    assert result["vendor_hdr_lfm_mode_validation"] == "not_established"
    np.testing.assert_array_equal(optical.data["photons"], original_photons)
    assert "mean_electrons" not in sensor.data


def test_underresolved_pwm_and_wrong_saved_mean_remain_inconclusive(optical_sensor):
    sensor, optical = optical_sensor
    aliased = _compare(
        sensor, optical, samples_per_exposure=(2, 4), maximum_relative_error=0.001
    )
    assert aliased["status"] == "inconclusive"
    assert aliased["comparisons"][-1]["maximum_relative_error_to_on_signal"] > 0.001

    wrong_saved_mean = np.zeros((8, 8), dtype=float)
    mismatched = _compare(
        sensor, optical, expected_lfm_mean_electrons=wrong_saved_mean
    )
    assert mismatched["status"] == "inconclusive"
    assert mismatched["saved_lfm_mean_relative_error"] > 0.1


def test_requires_resolved_authored_timing_and_bounded_sampling(optical_sensor):
    sensor, optical = optical_sensor
    sensor.fields["integration_time"] = 0.0010000005
    with pytest.raises(ValueError, match="integer nanosecond"):
        _compare(sensor, optical)
    sensor.fields["integration_time"] = 0.001
    with pytest.raises(ValueError, match="sample budget"):
        _compare(sensor, optical, samples_per_exposure=(128, 4096))
    with pytest.raises(ValueError, match="integer nanosecond"):
        _compare(sensor, optical, line_time_ns=20_000.5)


def test_numerical_psf_ringing_requires_explicit_bounded_correction(optical_sensor):
    sensor, optical = optical_sensor
    optical.data["photons"][0, 0, 0] = -0.0001
    with pytest.raises(ValueError, match="explicit bounded"):
        _compare(sensor, optical)
    result = _compare(sensor, optical, allow_numerical_negative_photons=True)
    correction = result["numerical_optical_correction"]
    assert correction["applied"] is True
    assert correction["negative_sample_count"] == 1
    assert correction["minimum_original_photons"] == -0.0001
    assert correction["maximum_relative_charge_change"] == 0
    assert optical.data["photons"][0, 0, 0] == -0.0001
    optical.data["photons"][0, 0, 0] = -1e6
    with pytest.raises(ValueError, match="roundoff bound"):
        _compare(sensor, optical, allow_numerical_negative_photons=True)


def test_four_phase_fixture_detects_one_bad_saved_mean(optical_sensor):
    sensor, optical = optical_sensor
    high = _source_mean(sensor, optical)
    means = np.stack([
        high * (
            0.02 + 0.98 * _on_fraction(
                np.arange(8) * 20e-6, 0.001, 0.002, 0.5, phase * 0.0005
            )[:, None]
        )
        for phase in range(4)
    ])
    kwargs = dict(
        period_ns=2_000_000, on_time_ns=1_000_000, line_time_ns=20_000,
        off_to_on_ratio=0.02, phase_seeds=[7] * 4,
    )
    passed = crosscheck_pwm_four_phase_set(
        sensor, optical, saved_mean_electrons=means, **kwargs
    )
    assert passed["status"] == "passed_at_declared_numerical_resolution"
    assert [item["phase_index"] for item in passed["phases"]] == list(range(4))
    assert passed["maximum_fine_temporal_relative_error"] < 0.003
    assert passed["physical_validation"] == "not_established"
    altered = means.copy()
    altered[2, 0, 0] = 0
    failed = crosscheck_pwm_four_phase_set(
        sensor, optical, saved_mean_electrons=altered, **kwargs
    )
    assert failed["status"] == "inconclusive"
    assert failed["phases"][2]["status"] == "inconclusive"


def test_saved_source_binds_npz_and_each_original_raw_phase(tmp_path):
    if not SOURCE.is_dir():
        pytest.skip("Bounded saved LFM source artifact is unavailable")
    report_path = SOURCE / "lens-sensor-lfm.json"
    npz_path = SOURCE / "lens-sensor-lfm.npz"
    saved = load_verified_lfm_artifact(
        report_path, npz_path, expected_report_sha256=SOURCE_REPORT_SHA
    )
    assert saved["original_pwm_raw_dn"].shape == (2, 4, 8, 8)
    assert saved["integrated_mean_electrons"].shape == (2, 4, 8, 8)
    assert saved["report"]["verification"]["physical_validation"] == "not_established"

    report = saved["report"].copy()
    raw = saved["original_pwm_raw_dn"].copy()
    raw[0, 0, 0, 0] += 1
    tampered_npz = tmp_path / "lens-sensor-lfm.npz"
    np.savez_compressed(
        tampered_npz,
        original_pwm_raw_dn=raw,
        original_constant_raw_dn=saved["original_constant_raw_dn"],
        integrated_mean_electrons=saved["integrated_mean_electrons"],
    )
    report["artifacts"] = {
        "npz_name": tampered_npz.name,
        "npz_sha256": hashlib.sha256(tampered_npz.read_bytes()).hexdigest(),
    }
    tampered_report = tmp_path / "lens-sensor-lfm.json"
    tampered_report.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="original RAW hash"):
        load_verified_lfm_artifact(
            tampered_report, tampered_npz,
            expected_report_sha256=hashlib.sha256(tampered_report.read_bytes()).hexdigest(),
        )
    with pytest.raises(ValueError, match="selected SHA-256"):
        load_verified_lfm_artifact(
            tampered_report, tampered_npz, expected_report_sha256=SOURCE_REPORT_SHA
        )

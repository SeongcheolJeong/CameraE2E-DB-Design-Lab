"""Independent, bounded checks for phase-aware RAW HDR reconstruction."""

from __future__ import annotations

import copy
import hashlib

import numpy as np
import pytest

from camera_platform.lens_sensor_lfm import BOUNDED_SCHEMA as BOUNDED_LFM_SCHEMA
from camera_platform.lens_sensor_lfm import SCHEMA as LFM_SCHEMA
from camera_platform.lens_sensor_lfm import TIMED_BOUNDED_SCHEMA as TIMED_LFM_SCHEMA
from camera_platform.lens_sensor_pwm_hdr import fuse_lens_sensor_pwm_hdr
from camera_platform.lens_sensor_timing_mode import (
    CAPTURE_PROGRAM,
    PHASE_ORIGIN,
    mode_binding,
    timing_provenance,
)
from camera_platform.lens_sensor_timing_mode import (
    SCHEMA as TIMING_MODE_SCHEMA,
)
from camera_platform.registry import digest
from camera_platform.saved_sensor_lfm import _on_fraction


def _fixture(
    *, fpn: bool = False, off_ratio: float = 0.02, rate_scale: float = 1.0,
    line_time_s: float = 20e-6, timed_mode: bool = False,
):
    rows = cols = 8
    exposures = [0.001, 0.004]
    period = 0.002
    duty = 0.5
    fractions = [0.0, 0.25, 0.5, 0.75]
    starts = np.arange(rows) * line_time_s
    readout = {
        "full_well_e": 10000.0,
        "dark_current_e_per_s": 10.0,
        "read_noise_e": 1.0,
        "gain": 1.0,
        "bit_depth": 12,
        "black_level_dn": 64.0,
    }
    dn_per_e = (4095 - 64) / 10000
    assumptions = {
        "prnu_fraction": 0.01 if fpn else 0.0,
        "dsnu_e": 2.0 if fpn else 0.0,
        "column_prnu_fraction": 0.005 if fpn else 0.0,
        "column_dsnu_e": 1.0 if fpn else 0.0,
    }
    # Independent synthetic virtual sensor signal: the underlying ON-light
    # rate varies by CFA site and across columns but is constant over phases.
    rate = rate_scale * (7e5 + 1e5 * (np.indices((rows, cols)).sum(axis=0) % 4))
    if fpn:
        streams = np.random.SeedSequence(23).spawn(4)
        pixel_offset_v = np.random.default_rng(streams[0]).normal(0, 2e-4, (rows, cols))
        pixel_gain = np.random.default_rng(streams[1]).normal(1, 0.01, (rows, cols))
        column_offset_v = np.random.default_rng(streams[2]).normal(0, 1e-4, (cols,))
        column_gain = np.random.default_rng(streams[3]).normal(1, 0.005, (cols,))
        gain = pixel_gain * column_gain[None, :]
        bias = (pixel_offset_v * column_gain[None, :] + column_offset_v[None, :]) / 1e-4
    else:
        gain, bias = np.ones((rows, cols)), np.zeros((rows, cols))
    raw = np.empty((2, 4, rows, cols), dtype=np.int32)
    cases = []
    for i, exposure in enumerate(exposures):
        frame_seed = int(np.random.SeedSequence([7, i]).generate_state(1)[0])
        phases = []
        for j, fraction in enumerate(fractions):
            on = _on_fraction(starts, exposure, period, duty, fraction * period)
            effective = exposure * (off_ratio + (1 - off_ratio) * on)
            electrons = rate * effective[:, None] + readout["dark_current_e_per_s"] * exposure
            raw[i, j] = np.clip(
                np.rint(
                    64 + dn_per_e * (np.clip(electrons * gain, 0, readout["full_well_e"]) + bias)
                ),
                0,
                4095,
            ).astype(np.int32)
            capture = {
                "schema": "camerae2e_sensor_capture_v1",
                "mode": "digital_raw",
                "noise_flag": 2,
                "exposure_time_s": exposure,
                "readout": readout,
                "dn_per_electron": dn_per_e,
                "black_level_dn": 64.0,
                "white_level_dn": 4095,
                "fixed_pattern_seed": 23,
                "fixed_pattern_order": (
                    "response_gain_then_well_clip_then_signed_bias_then_read_noise"
                ),
                "dsnu_definition": "fixed_readout_bias_per_capture_not_dark_current_variation",
                "noise_seed": frame_seed,
            }
            phases.append(
                {
                    "phase_fraction_of_period": fraction,
                    "phase_s": fraction * period,
                    "noise_seed": frame_seed,
                    "raw_sha256": hashlib.sha256(raw[i, j].tobytes(order="C")).hexdigest(),
                    "sensor_capture": capture,
                    "row_timing": {
                        "schema": "camerae2e_analytic_pwm_row_timing_v1",
                        "frame_start_s": 0.0,
                        "rolling_shutter": True,
                        "line_time_s": line_time_s,
                        "row_exposure_start_s": starts.tolist(),
                        "row_exposure_end_s": (starts + exposure).tolist(),
                        "ideal_pwm_on_fraction_per_row": on.tolist(),
                        "method": "exact_piecewise_constant_row_overlap",
                    },
                }
            )
        cases.append({"exposure_s": exposure, "phases": phases})
    report = {
        "schema": LFM_SCHEMA,
        "status": "completed",
        "lens_identity": {"revision_id": "lens-frozen"},
        "sensor_revision": {"run_id": "sensor-frozen"},
        "upstream_assets": {"sha256": "assets-frozen"},
        "linearity_check": {"status": "passed"},
        "source_boundary": {
            "saved_sensor_simulation_run_used": True,
            "saved_fpn_parameters_applied_to_generated_virtual_window_map": True,
            "physical_validation": "not_established",
        },
        "scene_contract": {"scene_kind": "uniform_d65", "spatial_motion": False},
        "geometry_provenance": {
            "saved_native_cfa": {
                "pattern": [[1, 2], [2, 3]],
                "filter_names": ["red", "green", "blue"],
                "pattern_index_base": 1,
                "native_size": [100, 100],
            },
            "configured_sensor_size": [rows, cols],
            "configured_sensor_scope": "virtual_simulation_window_only",
            "saved_native_array_size": [100, 100],
            "saved_native_array_crop_origin": None,
            "saved_native_array_crop_mapping": "not_established",
            "cfa_phase_basis": "saved_native_cfa_tile_anchored_at_virtual_origin_zero",
        },
        "controls": {
            "exposure_s": exposures,
            "pwm_period_s": period,
            "duty_cycle": duty,
            "off_to_on_ratio": off_ratio,
            "phase_fractions_of_period": fractions,
            "line_time_s": line_time_s,
            "seed": 7,
            "timing_provenance": "authored 20 us/line, not saved sensor timing",
            "sensor_size": [rows, cols],
            "saved_readout": readout,
            "saved_fpn_parameter_assumptions": assumptions,
            "fixed_pattern_seed": 23,
            "noise_seed_policy": (
                "same seed for four phases and the constant-light reference "
                "within each exposure; paired sensitivity, not independent repeats"
            ),
        },
        "cases": cases,
    }
    if timed_mode:
        mode = {
            "schema": TIMING_MODE_SCHEMA,
            "mode_id": "authored-40us-fixture",
            "binding": mode_binding(
                report["sensor_revision"], readout,
                {"profile": "generic_assumption", "classification": "unmeasured_assumption"},
                [rows, cols], exposures,
            ),
            "timing": {
                "shutter": "rolling", "line_time_s": line_time_s,
                "frame_start_s": 0.0, "phase_origin": PHASE_ORIGIN,
                "capture_program": CAPTURE_PROGRAM,
            },
            "provenance": {
                "source_kind": "authored_assumption",
                "source_uri": "authored://unit-fixture",
                "source_document_sha256": None,
                "method_and_version": "independent_synthetic_fixture_v1",
                "value_verification": "declared_not_independently_verified",
            },
        }
        sha = digest(mode)
        report["schema"] = TIMED_LFM_SCHEMA
        report["psf_compute_mode"] = "bounded_streaming"
        report["resource_preflight"] = {
            "scope": "central_bounded_strict_raytrace",
            "status": "within_development_budget",
            "execution_guaranteed": False,
        }
        report["psf_execution"] = {
            "mode": "strict_bounded_streaming",
            "complete_precomputed_stack": False,
            "peak_cached_kernels": 2,
        }
        report["source_boundary"].update(
            sensor_readout_profile="generic_assumption",
            sensor_readout_classification="unmeasured_assumption",
            timing_mode_source_kind="authored_assumption",
            timing_mode_source_document_sha256=None,
            timing_value_verification="declared_not_independently_verified",
        )
        report["controls"].update(
            timing_provenance=timing_provenance(mode),
            timing_mode=mode, timing_mode_sha256=sha,
            timing_mode_canonical_sha256=sha,
        )
        for case in report["cases"]:
            for phase in case["phases"]:
                phase["row_timing"].update(
                    schema="camerae2e_analytic_pwm_row_timing_v2",
                    timing_mode_sha256=sha,
                )
    return raw, report, rate


@pytest.mark.parametrize("fpn", [False, True])
def test_pwm_hdr_uses_row_phase_effective_time_and_original_raw(fpn):
    raw, report, rate = _fixture(fpn=fpn)
    original = raw.copy()
    result = fuse_lens_sensor_pwm_hdr(raw, report, conversion_gain_v_per_e=1e-4)
    arrays, summary = result["arrays"], result["report"]
    assert np.array_equal(raw, original)
    assert np.array_equal(arrays["original_pwm_raw_dn"], original)
    assert summary["original_pwm_raw_sha256"] == hashlib.sha256(raw.tobytes()).hexdigest()
    assert summary["fpn"]["physical_validation"] == "not_established"
    assert summary["waveform"]["timing_provenance"].startswith("authored")
    assert summary["status"] == "completed"
    assert summary["completion_scope"] == "all_pixels_estimable_with_fallback"
    assert summary["fused_hdr_pixels"] < summary["reconstructed_pixels"]
    assert summary["single_bracket_fallback_pixels"] > 0
    assert summary["temporal_consistency_unknown_pixels"] == (
        np.count_nonzero(arrays["temporal_consistency_unknown_mask"])
    )
    assert summary["virtual_geometry"]["saved_native_array_crop_mapping"] == "not_established"
    assert arrays["pwm_on_time_s"][0, 0, 0] == pytest.approx(0.001)
    assert arrays["pwm_on_time_s"][0, 2, 0] == 0
    assert not arrays["bracket_valid_mask"][0, 2, 0].any()
    assert not arrays["fused_valid_mask"][2, 0].any()
    assert arrays["single_bracket_fallback_mask"][2, 0].all()
    assert arrays["reconstructed_valid_mask"][2, 0].all()
    assert np.isnan(arrays["fused_on_luminance_rate"][2, 0]).all()
    assert np.count_nonzero(arrays["weights"][:, 2, 0]) == 0
    np.testing.assert_allclose(arrays["weights"][:, 0].sum(axis=0), 1.0)
    np.testing.assert_allclose(arrays["fused_on_luminance_rate"][0], rate, atol=2000, rtol=0)
    np.testing.assert_allclose(arrays["fused_on_luminance_rate"][1], rate, atol=2000, rtol=0)
    assert arrays["fused_estimated_variance"][arrays["fused_valid_mask"]].min() > 0


def test_zero_on_time_refused_even_with_nonzero_off_light():
    raw, report, _ = _fixture(off_ratio=0.5)
    result = fuse_lens_sensor_pwm_hdr(raw, report, conversion_gain_v_per_e=1e-4)
    arrays = result["arrays"]
    assert np.all(arrays["effective_illumination_time_s"][0, 2] > 0)
    assert not arrays["bracket_valid_mask"][0, 2, 0].any()
    assert not arrays["fused_valid_mask"][2, 0].any()
    assert arrays["single_bracket_fallback_mask"][2, 0].all()
    assert arrays["reconstructed_valid_mask"][2, 0].all()


def test_bounded_strict_psf_lfm_raw_can_be_fused_with_source_mode_preserved():
    raw, report, _ = _fixture()
    report.update(
        schema=BOUNDED_LFM_SCHEMA,
        psf_compute_mode="bounded_streaming",
        resource_preflight={
            "scope": "central_bounded_strict_raytrace",
            "status": "within_development_budget",
            "execution_guaranteed": False,
        },
        psf_execution={
            "mode": "strict_bounded_streaming",
            "complete_precomputed_stack": False,
            "peak_cached_kernels": 2,
        },
    )
    result = fuse_lens_sensor_pwm_hdr(raw, report, conversion_gain_v_per_e=1e-4)
    assert result["report"]["source_lfm_schema"] == BOUNDED_LFM_SCHEMA
    assert result["report"]["source_psf_compute_mode"] == "bounded_streaming"
    np.testing.assert_array_equal(result["arrays"]["original_pwm_raw_dn"], raw)
    changed = copy.deepcopy(report)
    changed["psf_execution"]["complete_precomputed_stack"] = True
    with pytest.raises(ValueError, match="bounded PSF execution evidence"):
        fuse_lens_sensor_pwm_hdr(raw, changed, conversion_gain_v_per_e=1e-4)


def test_selected_40us_mode_changes_row_integrals_and_hdr_uses_exact_timing():
    legacy_raw, legacy_report, _ = _fixture()
    raw, report, rate = _fixture(line_time_s=40e-6, timed_mode=True)
    assert not np.array_equal(raw, legacy_raw)
    before = raw.copy()
    result = fuse_lens_sensor_pwm_hdr(raw, report, conversion_gain_v_per_e=1e-4)
    np.testing.assert_array_equal(raw, before)
    assert result["report"]["source_lfm_schema"] == TIMED_LFM_SCHEMA
    assert result["report"]["waveform"]["line_time_s"] == 40e-6
    assert result["report"]["waveform"]["timing_mode_sha256"] == (
        report["controls"]["timing_mode_sha256"]
    )
    assert result["arrays"]["pwm_on_time_s"][0, 1, 7] != (
        fuse_lens_sensor_pwm_hdr(
            legacy_raw, legacy_report, conversion_gain_v_per_e=1e-4
        )["arrays"]["pwm_on_time_s"][0, 1, 7]
    )
    assert result["report"]["verification"]["physical_validation"] == "not_established"
    assert np.any(np.isfinite(result["arrays"]["reconstructed_on_luminance_rate"]))
    assert rate.shape == (8, 8)
    changed = copy.deepcopy(report)
    changed["controls"]["timing_mode"]["timing"]["line_time_s"] = 20e-6
    with pytest.raises(ValueError, match="timing mode differs"):
        fuse_lens_sensor_pwm_hdr(raw, changed, conversion_gain_v_per_e=1e-4)
    changed = copy.deepcopy(report)
    changed["controls"]["timing_mode"]["binding"]["saved_readout_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="timing mode differs"):
        fuse_lens_sensor_pwm_hdr(raw, changed, conversion_gain_v_per_e=1e-4)


def test_saturated_long_bracket_uses_labeled_short_fallback():
    raw, report, rate = _fixture(rate_scale=6.0)
    result = fuse_lens_sensor_pwm_hdr(raw, report, conversion_gain_v_per_e=1e-4)
    arrays, summary = result["arrays"], result["report"]
    fallback = arrays["single_bracket_fallback_mask"]
    assert fallback[0].any()
    assert np.all(arrays["single_bracket_fallback_source_exposure_index"][0][fallback[0]] == 0)
    assert np.all(arrays["reconstruction_weights"][0, 0][fallback[0]] == 1)
    assert np.all(arrays["reconstruction_weights"][1, 0][fallback[0]] == 0)
    np.testing.assert_allclose(
        arrays["reconstructed_on_luminance_rate"][0][fallback[0]],
        rate[fallback[0]],
        atol=2000,
        rtol=0,
    )
    assert np.isfinite(arrays["reconstructed_estimated_variance"][fallback]).all()
    assert summary["single_bracket_fallback_pixels"] == np.count_nonzero(fallback)
    assert summary["fused_hdr_pixels"] == np.count_nonzero(arrays["fused_valid_mask"])


def test_tampered_raw_timing_cfa_or_fpn_contract_is_refused():
    raw, report, _ = _fixture()
    changed = raw.copy()
    changed[0, 0, 0, 0] += 1
    with pytest.raises(ValueError, match="hash"):
        fuse_lens_sensor_pwm_hdr(changed, report, conversion_gain_v_per_e=1e-4)
    modified = copy.deepcopy(report)
    modified["cases"][0]["phases"][0]["row_timing"]["ideal_pwm_on_fraction_per_row"][0] = 0.1
    with pytest.raises(ValueError, match="row windows"):
        fuse_lens_sensor_pwm_hdr(raw, modified, conversion_gain_v_per_e=1e-4)
    modified = copy.deepcopy(report)
    modified["geometry_provenance"]["saved_native_cfa"]["pattern"] = [[1, 1], [2, 3]]
    with pytest.raises(ValueError, match="RGGB"):
        fuse_lens_sensor_pwm_hdr(raw, modified, conversion_gain_v_per_e=1e-4)
    modified = copy.deepcopy(report)
    modified["controls"]["saved_fpn_parameter_assumptions"].pop("dsnu_e")
    with pytest.raises(ValueError, match="four saved FPN"):
        fuse_lens_sensor_pwm_hdr(raw, modified, conversion_gain_v_per_e=1e-4)
    modified = copy.deepcopy(report)
    modified["controls"]["line_time_s"] = 40e-6
    with pytest.raises(ValueError, match="frozen authored row timing"):
        fuse_lens_sensor_pwm_hdr(raw, modified, conversion_gain_v_per_e=1e-4)

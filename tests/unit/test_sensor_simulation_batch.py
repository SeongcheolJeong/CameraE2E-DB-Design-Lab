"""Actual bounded sensor simulations and truthful unsupported capabilities."""

import json

import numpy as np
import pytest

from pyisetcam.sensor_simulation_batch import (
    resolved_simulation_settings,
    simulate_sensor_record,
)


@pytest.fixture(scope="module")
def physical_result():
    return simulate_sensor_record("DEF-2206-805", settings={"size": 8})


def test_physical_grid_and_domain_limits_are_explicit(physical_result):
    data, arrays = physical_result["metadata"], physical_result["arrays"]
    assert data["status"] == "simulated"
    assert data["model_kind"] == "physical_estimate_v1"
    assert data["checks"]["numerical_passed"]
    assert data["measured_accuracy_verified"] is False
    np.testing.assert_array_equal(arrays["wave_nm"], np.arange(400, 701, 10))
    assert arrays["spectral_eqe"].shape == (31, 4)
    assert arrays["angular_eqe"].shape == (13, 2, 31, 4)
    assert arrays["pupil_eqe"].shape == (16, 31, 4)
    requests = data["optical_capabilities"]["pupil_requests"]
    limited = [p for p in requests if p["status"] == "limited"]
    assert len(limited) == 2
    assert all(p["fnumber"] == 2 and p["cra_deg"] == 40 for p in limited)
    assert all(p["continuous_pupil_max_theta_deg"] > 60 for p in limited)
    assert all("array_index" not in p for p in limited)
    np.testing.assert_allclose(
        arrays["angular_R"] + arrays["angular_T"] + arrays["angular_A"], 1, atol=1e-14
    )
    np.testing.assert_allclose(arrays["angular_applied_routing_acceptance"], 1)
    assert "material_bundle" in data["model_snapshot"]
    assert data["readout_profile"]["readout"]["full_well_e"] == 38200
    json.dumps(data, allow_nan=False)


def test_raw_capture_reuses_linear_means_and_retains_native_sites(physical_result):
    data, arrays = physical_result["metadata"], physical_result["arrays"]
    assert len(data["cases"]) == 9
    assert data["sample_count"] == 88
    assert data["optical_solve_count"] == 4
    for offset in (0, 3, 6):
        a, b, c = [arrays[f"case_{i:03d}_mean_electrons"] for i in range(offset, offset + 3)]
        np.testing.assert_allclose(b, 4 * a, rtol=1e-14)
        np.testing.assert_allclose(c, 16 * a, rtol=1e-14)
    assert data["checks"]["independent_double_exposure"]["maximum_relative_error"] == 0
    assert data["isp"]["available"]
    assert arrays["case_000_linear_rgb"].shape == (8, 8, 3)
    for case in data["cases"]:
        assert len(case["sites"]) == 4
        assert case["capture"]["fixed_pattern_seed"] == 0
        assert case["noise_check"]["passed"]
        assert case["snr_eligible"]
        raw = arrays[case["case_id"] + "_raw_dn"]
        assert raw.dtype == np.int32 and raw.shape == (8, 8)
        assert 0 <= raw.min() <= raw.max() <= 4095
    assert data["illuminant_provenance"]["F11"]["bin_width_nm"] == 10
    assert all(np.all(np.isfinite(value)) for value in arrays.values())


def test_dark_noise_and_bright_clip_do_not_become_valid_snr(physical_result):
    data = physical_result["metadata"]
    dark, bright = data["stress_cases"]
    assert dark["mean_photoelectrons"] == 0
    assert dark["mean_dark_electrons"] == 10
    assert dark["noise_check"]["passed"]
    assert dark["temporal_snr_db"] is None and not dark["snr_eligible"]
    assert data["checks"]["dark_count"]["maximum_absolute_error_electrons"] < 1e-10
    assert bright["saturation"]["well_fraction"] > 0.99
    assert bright["noise_check"]["eligible"] is False
    assert bright["noise_check"]["passed"] is None
    assert bright["temporal_snr_db"] is None and not bright["snr_eligible"]


@pytest.mark.parametrize(
    "code,channels,shape",
    [
        ("DEF-2002-801", 3, (4, 4)),
        ("DEF-1902-803", 1, (1, 1)),
        ("DEP-1902-801", 1, (1, 1)),
    ],
)
def test_db_fallback_preserves_quad_or_mono_and_does_not_invent_angles(code, channels, shape):
    result = simulate_sensor_record(
        code,
        settings={
            "size": 8,
            "nframes": 3,
            "illuminants": ["D65"],
            "exposure_s": [0.004],
        },
    )
    data, arrays = result["metadata"], result["arrays"]
    assert data["model_kind"] == "db_estimate_v1"
    assert data["checks"]["numerical_passed"]
    assert arrays["spectral_eqe"].shape == (31, channels)
    assert np.shape(data["native_cfa"]["pattern"]) == shape
    assert len(data["cases"][0]["sites"]) == shape[0] * shape[1]
    assert not data["optical_capabilities"]["angular_response_supported"]
    assert not data["isp"]["available"]
    assert not any(k.startswith("angular_") or k.startswith("pupil_") for k in arrays)
    assert data["readout_profile"]["provenance"]["classification"] == "unmeasured_assumption"
    json.dumps(data, allow_nan=False)


def test_same_seed_reproduces_actual_raw_and_changed_seed_preserves_photons():
    settings = {"size": 8, "nframes": 3, "illuminants": ["D65"], "exposure_s": [0.004]}
    first = simulate_sensor_record("DEF-1902-803", settings=settings)
    again = simulate_sensor_record("DEF-1902-803", settings=settings)
    changed = simulate_sensor_record("DEF-1902-803", settings={**settings, "seed": 17})
    np.testing.assert_array_equal(
        first["arrays"]["case_000_raw_dn"], again["arrays"]["case_000_raw_dn"]
    )
    np.testing.assert_array_equal(
        first["arrays"]["case_000_mean_electrons"], changed["arrays"]["case_000_mean_electrons"]
    )
    assert not np.array_equal(
        first["arrays"]["case_000_raw_dn"], changed["arrays"]["case_000_raw_dn"]
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"size": 10},
        {"size": 4},
        {"nframes": 1},
        {"nframes": "8"},
        {"exposure_s": [0]},
        {"theta_deg": [61]},
        {"pupil_fnumbers": [float("nan")]},
        {"illuminants": ["unknown"]},
        {"seed": -1},
        {"surprise": 1},
    ],
)
def test_global_settings_fail_before_source_lookup(settings):
    with pytest.raises(ValueError):
        simulate_sensor_record("not-a-sensor", settings=settings)


def test_settings_are_json_safe_resolved_and_independent():
    first = resolved_simulation_settings()
    assert first["size"] == 32 and first["nframes"] == 8
    json.dumps(first, allow_nan=False)
    first["illuminants"].clear()
    assert resolved_simulation_settings()["illuminants"] == ["D65", "A", "F11"]


def test_failed_noise_validation_quarantines_but_preserves_arrays(monkeypatch):
    import pyisetcam.sensor_simulation_batch as engine

    original = engine.resample_sensor_capture

    def broken_readout(sensor, *, seed):
        result = original(sensor, seed=seed)
        result.data["raw_dn"][:] = 0
        return result

    monkeypatch.setattr(engine, "resample_sensor_capture", broken_readout)
    result = engine.simulate_sensor_record(
        "DEF-1902-803",
        settings={
            "size": 8,
            "nframes": 3,
            "illuminants": ["D65"],
            "exposure_s": [0.004],
        },
    )
    assert result["metadata"]["status"] == "quarantined"
    assert not result["metadata"]["checks"]["numerical_passed"]
    assert not result["metadata"]["checks"]["temporal_noise"]["passed"]
    assert result["arrays"]["case_000_raw_dn"].shape == (8, 8)


def test_source_reviewed_four_cell_correction_uses_quad_raw_without_fake_angle():
    result = simulate_sensor_record(
        "DEF-2403-801",
        settings={
            "size": 8,
            "nframes": 3,
            "illuminants": ["D65"],
            "exposure_s": [0.004],
        },
    )
    data = result["metadata"]
    assert data["status"] == "simulated"
    assert data["model_kind"] == "db_estimate_v1"
    assert data["source_scope_policy"]["action"] == "override"
    assert data["source_scope_policy"]["evidence"]
    assert data["model_snapshot"]["cfa_pattern"] == "quad_bayer"
    assert data["config"]["requires_frozen_model_snapshot"]
    np.testing.assert_array_equal(
        data["native_cfa"]["pattern"], [[1, 1, 2, 2], [1, 1, 2, 2], [2, 2, 3, 3], [2, 2, 3, 3]]
    )
    assert len(data["cases"][0]["sites"]) == 16
    assert not data["optical_capabilities"]["angular_response_supported"]


@pytest.mark.parametrize("code", ["DEF-1809-802", "DEF-2502-802", "DEF-2204-804"])
def test_known_polarized_uv_and_nonacell_sources_are_excluded(code):
    with pytest.raises(ValueError, match="excluded by source-scope policy"):
        simulate_sensor_record(code)

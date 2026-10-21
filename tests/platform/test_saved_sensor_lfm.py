"""A finite PWM phase study has an exact independent row-time integral check."""

from __future__ import annotations

import numpy as np
import pytest

from camera_platform.saved_sensor_lfm import _on_fraction, _phase_modulation
from camera_platform.saved_sensor_lfm_job import default_spec, specification


def test_exact_square_wave_on_time_per_row_and_phase():
    starts = np.array([0.0, 0.0005, 0.001, 0.0015])
    np.testing.assert_allclose(
        _on_fraction(starts, 0.001, 0.002, 0.5, 0.0),
        [1.0, 0.5, 0.0, 0.5],
    )
    np.testing.assert_allclose(
        _on_fraction(starts, 0.004, 0.002, 0.5, 0.0005),
        [0.5, 0.5, 0.5, 0.5],
    )


def test_lfm_spec_requires_acknowledged_bounded_phase_sweep():
    spec = default_spec()
    spec["sensor_revision"] = {"identifier": "DEF-1801-802", "run_id": "run-a"}
    with pytest.raises(ValueError, match="acknowledgement"):
        specification(spec)
    spec["acknowledge_simulation_limits"] = True
    assert specification(spec)["pwm_period_s"] == 0.002
    with pytest.raises(ValueError, match="distinct and ascending"):
        specification(dict(spec, exposure_s=[0.004, 0.001]))
    with pytest.raises(ValueError, match="32-cycle"):
        specification(dict(spec, exposure_s=[0.001, 0.02], pwm_period_s=0.0005))
    with pytest.raises(ValueError, match="on_luminance_cd_m2"):
        specification(dict(spec, on_luminance_cd_m2=10**1000))


def test_phase_ratio_is_unavailable_at_black_floor_or_any_clipping():
    def capture(*, well=0.0, white=0.0, zero=0.0):
        return {"saturation": {
            "well_fraction": well,
            "adc_white_fraction": white,
            "adc_zero_fraction": zero,
        }}

    dim = [
        {"mean_raw_dn": value, "sensor_capture": capture()}
        for value in (64.0, 64.5, 64.0, 64.5)
    ]
    result = _phase_modulation(dim, capture(), 64.0)
    assert result["phase_modulation_status"] == (
        "unavailable_below_black_relative_signal_floor"
    )
    assert result["phase_modulation_fraction_black_relative"] is None
    assert result["phase_mean_dn_range"] == 0.5

    clipped = [
        {"mean_raw_dn": value, "sensor_capture": capture(well=0.1)}
        for value in (4000.0, 4000.0, 4000.0, 4000.0)
    ]
    result = _phase_modulation(clipped, capture(), 64.0)
    assert result["phase_modulation_status"] == "unavailable_clipped_or_rail_limited"
    assert result["phase_modulation_fraction_black_relative"] is None
    assert result["phase_mean_dn_range"] == 0.0

    healthy = [
        {"mean_raw_dn": value, "sensor_capture": capture()}
        for value in (100.0, 110.0, 100.0, 110.0)
    ]
    result = _phase_modulation(healthy, capture(), 64.0)
    assert result["phase_modulation_status"] == "eligible"
    assert result["phase_modulation_fraction_black_relative"] == pytest.approx(10 / 41)

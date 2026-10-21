"""Noisy native SFR admission is explicit and never clips the measurements."""
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import calibration_lab_engine as engine


def fixture():
    profile = engine.default_profile()
    f = np.linspace(0, 0.25, 41)
    values = np.exp(-2 * np.pi**2 * 1.3**2 * f**2)
    values[1], values[2] = 1.02, 1.01
    record = {"id": "synthetic-noisy-sfr", "chart": "mtf", "role": "training",
        "source_kind": "synthetic", "measurement_domain": "camera_raw",
        "conditions": deepcopy(profile["conditions"]),
        "observations": {"frequency_cy_px": f.tolist(), "mtf": values.tolist(),
                         "field_xy": [0, 0], "orientation_deg": 5}}
    return profile, record


def test_native_sfr_above_one_requires_opt_in_and_preserves_every_value():
    profile, record = fixture()
    original = deepcopy(record)
    with pytest.raises(ValueError, match="above 1"):
        engine.fit_profile(profile, [record])
    fit = engine.fit_profile(profile, [record], allow_sfr_overshoot=True)
    diagnostic = next(x for x in fit["diagnostics"] if x["chart"] == "mtf")
    assert record == original
    assert diagnostic["observed_sfr_max"] == 1.02
    assert diagnostic["observed_sfr_overshoot_samples"] == 2
    assert diagnostic["observed_sfr_clipped"] is False
    assert diagnostic["radial_inverse_roundtrip_passed"] is None
    assert diagnostic["radial_inverse_status"] == "unavailable_observed_sfr_exceeds_passive_mtf_domain"
    assert diagnostic["rmse"] > 0


@pytest.mark.parametrize("corruption", ["negative", "nan", "dc", "frequency"])
def test_opt_in_retains_normalization_frequency_and_finiteness_guards(corruption):
    profile, record = fixture()
    obs = record["observations"]
    if corruption == "negative":
        obs["mtf"][4] = -0.1
    elif corruption == "nan":
        obs["mtf"][4] = float("nan")
    elif corruption == "dc":
        obs["mtf"][0] = 1.1
    else:
        obs["frequency_cy_px"][4] = obs["frequency_cy_px"][3]
    with pytest.raises(ValueError):
        engine.fit_profile(profile, [record], allow_sfr_overshoot=True)

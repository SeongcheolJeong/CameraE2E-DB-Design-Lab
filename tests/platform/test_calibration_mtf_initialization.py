"""External directional MTF fitting must not be trapped by periodic prior angles."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import calibration_lab_engine as engine
from camera_platform.evidence_objects import pack


@pytest.mark.parametrize("prior_angle", [89.9999991414453, -89.9999991414453, 0.0, 179.9])
def test_directional_mtf_recovers_truth_from_boundary_or_isotropic_prior(prior_angle):
    base = engine.default_profile()
    base["blur"]["field_nodes"][0].update(
        sigma_x_px=0.7880643075595525,
        sigma_y_px=0.7880643068130583,
        theta_deg=prior_angle,
    )
    truth = {"sigma_x_px": 0.85, "sigma_y_px": 1.15, "theta_deg": 18.0}
    frequency = np.linspace(0, 0.5, 33)
    measurements = []
    for angle in (0.0, 45.0, 90.0, 135.0):
        measurements.append(
            {
                "chart": "mtf",
                "source_kind": "synthetic",
                "measurement_domain": "camera_raw",
                "role": "training",
                "conditions": deepcopy(base["conditions"]),
                "observations": {
                    "frequency_cy_px": frequency.tolist(),
                    "mtf": engine._mtf(truth, frequency, angle).tolist(),
                    "orientation_deg": angle,
                    "field_xy": [0.0, 0.0],
                },
            }
        )
    fit = engine.fit_profile(base, measurements)
    node = fit["profile"]["blur"]["field_nodes"][0]
    assert node["sigma_x_px"] == pytest.approx(0.85, abs=1e-7)
    assert node["sigma_y_px"] == pytest.approx(1.15, abs=1e-7)
    assert node["theta_deg"] == pytest.approx(18, abs=1e-6)
    assert fit["diagnostics"][0]["rmse"] < 1e-9
    # Different frequencies and directions test the identified covariance,
    # beyond re-evaluating only the training samples.
    for angle in (18, 27, 68, 117, 173):
        holdout_frequency = np.linspace(0.007, 0.493, 37)
        np.testing.assert_allclose(
            engine._mtf(node, holdout_frequency, angle),
            engine._mtf(truth, holdout_frequency, angle),
            atol=1e-9,
        )
    assert pack(fit)["objects"]


def test_rank_deficient_log_covariance_keeps_declared_fallback():
    frequency = np.linspace(0, 0.5, 33)
    truth = {"sigma_x_px": 0.85, "sigma_y_px": 1.15, "theta_deg": 18.0}
    curves = [(frequency, engine._mtf(truth, frequency, 0), 0, {})] * 3
    prior = [1.0, 1.0, 89.99]
    assert engine._analytic_mtf_initial(curves, prior) == prior

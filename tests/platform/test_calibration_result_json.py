"""Fitted chart results must pass the same strict evidence encoding as worker finish."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import calibration_lab_engine as engine
from camera_platform.evidence_objects import pack


@pytest.mark.parametrize("sampled", [False, True])
def test_fitted_mtf_profile_and_fresh_comparison_are_strict_json(tmp_path, sampled):
    baseline = engine.default_profile()
    baseline["blur"]["field_nodes"][0]["field_xy"] = [0.25, -0.5]
    if sampled:
        baseline["blur"].update(
            family="field_effective_psf_v1",
            field_application="explicit_nodes_only",
            included_effects=["lens_blur", "pixel_aperture"],
            charge_diffusion_assumption="zero_not_identified",
        )
        baseline["blur"]["field_nodes"][0].update(
            family="sampled_native_kernel_v1",
            kernel=[[0.0, 0.1, 0.0], [0.2, 0.4, 0.1], [0.0, 0.1, 0.1]],
            kernel_pitch_px=1.0,
            sampling="native_cell_center_pixel_integrated",
        )
    frequency = np.linspace(0, 0.5, 32)
    measurement = {
        "id": "mtf-json-result",
        "chart": "mtf",
        "source_kind": "synthetic",
        "measurement_domain": "camera_raw",
        "role": "training",
        "conditions": deepcopy(baseline["conditions"]),
        "observations": {
            "frequency_cy_px": frequency.tolist(),
            "mtf": np.exp(-2 * np.pi**2 * 1.3**2 * frequency**2).tolist(),
            "field_xy": [0.25, -0.5],
            "orientation_deg": 0.0,
        },
    }
    fit = engine.fit_profile(baseline, [measurement])
    summary = engine.run_comparison(
        baseline, fit["profile"], [measurement], tmp_path / "comparison", seed=7
    )
    summary.update(fit=fit, baseline_profile=baseline, candidate_profile=fit["profile"])
    # json.dumps alone is insufficient: NumPy float64 subclasses pass it, but
    # the registry evidence contract intentionally permits only native types.
    encoded = pack({"summary": summary, "artifacts": []})
    assert encoded["objects"]
    assert all(type(v) is float for v in fit["profile"]["blur"]["field_nodes"][0]["field_xy"])
    assert fit["profile"]["blur"]["field_nodes"][0]["field_xy"] == [0.25, -0.5]

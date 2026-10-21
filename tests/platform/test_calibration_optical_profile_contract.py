"""Bound the optical-only effective blur and its supported field domain."""

import pytest

from camera_platform import calibration_lab_engine as engine


def test_lens_receiver_blur_requires_explicit_zero_diffusion_assumption():
    profile = engine.default_profile()
    profile["blur"]["included_effects"] = ["lens_blur", "pixel_aperture"]
    with pytest.raises(ValueError, match="declared spatial effects"):
        engine.validate_profile(profile)
    profile["blur"]["charge_diffusion_assumption"] = "zero_not_identified"
    assert engine.validate_profile(profile)["blur"] == profile["blur"]
    profile["blur"]["charge_diffusion_assumption"] = "measured"
    with pytest.raises(ValueError, match="declared spatial effects"):
        engine.validate_profile(profile)


def test_optical_center_does_not_extrapolate_to_other_sensor_fields():
    profile = engine.default_profile()
    profile["blur"].update(
        included_effects=["lens_blur", "pixel_aperture"],
        charge_diffusion_assumption="zero_not_identified",
        field_application="explicit_nodes_only",
    )
    checked = engine.validate_profile(profile)
    assert engine._field_node(checked, {"field_xy": [0, 0]}) == profile["blur"]["field_nodes"][0]
    with pytest.raises(ValueError, match="No effective PSF"):
        engine._field_node(checked, {"field_xy": [0.7, 0.7]})

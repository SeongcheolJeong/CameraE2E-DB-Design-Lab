"""Different solver grids must never loosen the physical Lens identity."""

from copy import deepcopy

import pytest

from camera_platform import lens_factory, prescription_psf
from camera_platform.prescription_identity import (
    SCHEMA,
    sampling_independent_binding,
)
from camera_platform.registry import digest


@pytest.mark.parametrize("version", [3, 4, 5])
def test_independent_numerical_sampling_preserves_both_full_request_hashes(version):
    template = prescription_psf.request_template(version)
    parent = lens_factory.validate_request(template["prescription"])
    psf = deepcopy(parent)
    psf["sampling"].update(polar_rings=20, azimuth_samples=20)
    psf = lens_factory.validate_request(psf)
    assert sampling_independent_binding(parent, parent) is None
    binding = sampling_independent_binding(parent, psf)
    assert binding == {
        "schema": SCHEMA,
        "physical_prescription_sha256": binding["physical_prescription_sha256"],
        "radiometry_request_sha256": digest(parent),
        "psf_prescription_request_sha256": digest(psf),
        "excluded_numerical_field": "sampling",
    }
    assert len(binding["physical_prescription_sha256"]) == 64


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_prescriptions_still_require_the_exact_full_request(version):
    template = prescription_psf.request_template(version)
    parent = lens_factory.validate_request(template["prescription"])
    psf = deepcopy(parent)
    psf["sampling"]["polar_rings"] = 20
    with pytest.raises(ValueError, match="v3-v5"):
        sampling_independent_binding(parent, psf)


@pytest.mark.parametrize(
    "physical_change",
    [
        lambda p: (
            p["wavelength_nm"].__setitem__(0, 499.0),
            p["stop_transmission"]["wavelength_nm"].__setitem__(0, 499.0),
        ),
        lambda p: (
            p["image_height_mm"].__setitem__(1, 0.4),
            p["image_height_mm"].__setitem__(2, 0.8),
        ),
        lambda p: p["surfaces"][-1].__setitem__(
            "thickness_mm", p["surfaces"][-1]["thickness_mm"] + 0.01
        ),
        lambda p: p["stop_transmission"]["power_transmittance"][0].__setitem__(0, 0.9),
        lambda p: p["surfaces"][1].__setitem__(
            "radius_mm", p["surfaces"][1]["radius_mm"] + 0.1
        ),
    ],
    ids=["wavelength", "field", "focus", "stop", "surface"],
)
def test_valid_changes_to_physical_input_are_rejected(physical_change):
    parent = lens_factory.validate_request(prescription_psf.request_template(4)["prescription"])
    psf = deepcopy(parent)
    psf["sampling"].update(polar_rings=20, azimuth_samples=20)
    physical_change(psf)
    psf = lens_factory.validate_request(psf)
    with pytest.raises(ValueError, match="same prescription"):
        sampling_independent_binding(parent, psf)


def test_units_and_undeclared_request_fields_are_rejected():
    parent = lens_factory.validate_request(prescription_psf.request_template(4)["prescription"])
    for change in (lambda p: p.update(length_unit="um"), lambda p: p.update(extra=1)):
        psf = deepcopy(parent)
        psf["sampling"].update(polar_rings=20, azimuth_samples=20)
        change(psf)
        with pytest.raises(ValueError):
            sampling_independent_binding(parent, psf)

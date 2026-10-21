"""Pixel transport must remain independently traceable through the response chain."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from run_camera_response_comparison import (  # noqa: E402
    PITCH,
    PIXELS,
    WAVE,
    base_request,
    native_response,
    pupil_lens,
)

from camera_platform import electrical, spatial  # noqa: E402
from camera_platform.registry import digest  # noqa: E402
from camera_platform.response_chain import evaluate_response  # noqa: E402


def _request_with_collection():
    psf = spatial.request_template()["psf"]
    psf.update(wavelength_nm=WAVE, sample_pitch_um=PITCH, values=[[[1.0]], [[1.0]]])
    lens, _ = pupil_lens(4.0)
    request = base_request(psf, lens, native_response(PIXELS["angular_sensitive"]),
                           1e-4, 1.0)
    response = request["native_pixel_response"]
    response["schema"] = "camera_native_generation_response_v1"
    response["response_basis"] = (
        "independent_site_generated_electrons_per_incident_pixel_photon"
    )
    response["source"]["description"] = "Synthetic generation response before transport"
    request["charge_collection"] = {
        "schema": "camera_charge_collection_v1",
        "site_names": ["R", "Gr", "Gb", "B"],
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": PITCH,
        "wavelength_nm": WAVE.copy(),
        "destination_offsets_rc": [[0, 0]],
        "values_axis_order": ["wavelength", "source_site", "destination_offset"],
        "probabilities": [[[1.0] for _ in range(4)] for _ in WAVE],
        "interpolation": "linear_wavelength",
        "input_plane": "silicon_generated_charge",
        "output_plane": "collection_well_before_readout",
        "temporal_model": "instantaneous_memoryless",
        "boundary": "absorb_outside_full_sensor",
        "angular_assumption": "direction_independent_depth_marginal",
        "statistics": "single_destination_poisson_thinning",
        "regime": "linear_low_injection",
        "source": {"kind": "simulated", "description": "Synthetic identity carrier routing"},
    }
    return request


def test_charge_collection_has_independent_identity_and_changes_raw():
    first_request = _request_with_collection()
    second_request = deepcopy(first_request)
    second_request["charge_collection"]["probabilities"] = (
        [[[0.5] for _ in range(4)] for _ in WAVE]
    )
    model = electrical.default_electrical_model(first_request["readout"])
    first = evaluate_response(first_request, model, exposure_s=.004, analog_gain=1.0)
    second = evaluate_response(second_request, model, exposure_s=.004, analog_gain=1.0)
    first_id = first["summary"]["response_identity"]
    second_id = second["summary"]["response_identity"]
    for key in ("psf", "lens", "pixel_response", "electrical_model", "scene_array_sha256"):
        assert first_id[key] == second_id[key]
    assert first_id["charge_collection"] == digest(first_request["charge_collection"])
    assert second_id["charge_collection"] == digest(second_request["charge_collection"])
    assert first_id["charge_collection"] != second_id["charge_collection"]
    np.testing.assert_allclose(
        second["arrays"]["mean_photocharge_e"],
        first["arrays"]["mean_photocharge_e"] * 0.5,
        rtol=1e-14,
    )
    assert np.any(second["arrays"]["raw_dn"] != first["arrays"]["raw_dn"])


def test_invalid_electrical_conditions_fail_before_optical_work(monkeypatch):
    request = _request_with_collection()
    model = electrical.default_electrical_model(request["readout"])
    model["dark_current_doubling_c"] = 1e-6
    monkeypatch.setattr(spatial, "photoelectron_rate",
                        lambda _: pytest.fail("invalid readout reached optical integration"))
    with pytest.raises(ValueError, match="Dark-current temperature scaling"):
        evaluate_response(request, model, exposure_s=.004, analog_gain=1.0,
                          temperature_c=1000.0)

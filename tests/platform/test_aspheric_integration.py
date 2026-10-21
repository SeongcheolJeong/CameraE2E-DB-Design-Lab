"""Versioned asphere direction contract and native runtime compatibility."""

from copy import deepcopy

import numpy as np
import pytest
from test_native_pixel_workflow import _pure_request

from camera_platform import factory, lens_direction_response, spatial


def test_new_direction_symmetry_cannot_be_substituted_in_legacy_schema():
    request = _pure_request()
    old = request["lens"]["directional_response"]
    new = deepcopy(old)
    new.update(
        schema="camera_lens_directional_response_v2",
        symmetry="centered_even_asphere_or_spherical_or_plane_prescription_radial_stop",
    )
    assert lens_direction_response.validate_directional_response(new) == new
    assert lens_direction_response.validate_directional_response(old) == old
    for version, symmetry in [(old["schema"], new["symmetry"]), (new["schema"], old["symmetry"])]:
        with pytest.raises(ValueError, match="symmetry"):
            lens_direction_response.validate_directional_response(
                {**new, "schema": version, "symmetry": symmetry}
            )


def test_explicit_new_symmetry_preserves_joint_native_quadrature_and_raw_semantics():
    request = _pure_request()
    baseline = spatial.SpatialSceneRuntime().execute(request)
    extended = deepcopy(request)
    extended["lens"]["directional_response"].update(
        schema="camera_lens_directional_response_v2",
        symmetry="centered_even_asphere_or_spherical_or_plane_prescription_radial_stop",
    )
    actual = spatial.SpatialSceneRuntime().execute(extended)
    # Identical authored direction/power samples must retain their meaning,
    # irrespective of the class of centered surface that generated them.
    for name in ("channel_electrons", "raw_frames", "rgb_frames"):
        np.testing.assert_array_equal(actual["arrays"][name], baseline["arrays"][name])


def test_only_prescription_factories_advertise_aspheric_request_version():
    for plugin in factory.list_plugins():
        assert (4 in plugin["request_versions"]) == (
            plugin["plugin_id"] in {"rayoptics_lens_v1", "prescription_psf_v1"}
        )

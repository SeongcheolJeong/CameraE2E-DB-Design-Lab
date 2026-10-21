"""Located ROI offset is measured from images with fixed supported context."""
import numpy as np
import pytest
from scipy.ndimage import uniform_filter1d
from scipy.special import erf

from camera_platform import db_design_sensor_shutter_metrics as metrics
from camera_platform.db_design_shutter_metrics import geometry_from_planes as legacy_geometry


def _edge(shift=0, slope=.08, blur=27):
    y, x = np.indices((256, 256), dtype=float)
    image = .08 + .67 * (1 + erf((x - 128 - slope * (y - 127.5) - shift) / 2)) / 2
    image = uniform_filter1d(image, blur, axis=1, mode='nearest')
    return image[..., None] * np.array([.7, 1, .9, .5])


def _geometry(actual, baseline, offset=-12, skew=8.7, valid=None):
    return metrics.geometry_from_planes(actual, baseline,
        valid_rect=valid or [8, 248, 60, 196], edge_roi=[108, 148, 108, 148],
        theoretical_skew_px=skew, theoretical_center_displacement_px=offset)


def test_shifted_long_exposure_edge_has_supported_fixed_search_and_independent_intercept():
    baseline = _edge()
    actual = _edge(shift=-12, slope=.08 + 8.7 / 255)
    measured = _geometry(actual, baseline)
    assert measured['status'] == 'ok'
    assert measured['search_roi_row_col'] == [108, 148, 84, 172]
    assert measured['measured_roi_center_displacement_px'] == pytest.approx(-12, abs=.001)
    assert measured['measured_local_roi_skew_px'] == pytest.approx(8.7, abs=.005)
    assert measured['measured_observed_skew_px'] == pytest.approx(8.7 * 39 / 255, abs=.001)
    assert measured['measured_active_array_skew_extrapolated_px'] == pytest.approx(8.7 * 1096 / 255, abs=.03)
    misleading = _geometry(actual, baseline, offset=999, skew=888)
    for key in ('measured_roi_center_displacement_px', 'measured_local_roi_skew_px', 'rs_edge_x_px'):
        assert misleading[key] == measured[key]
    # The old narrower search can clip the translated, blurred tail. Its API
    # remains unchanged, but it is not reused for the newly located ROI metric.
    old = legacy_geometry(actual, baseline, valid_rect=[8, 248, 60, 196],
        edge_roi=[108, 148, 108, 148], theoretical_skew_px=8.7)
    assert abs(old['midpoint_relative_offset_px'] + 12) > .01


def test_zero_shift_and_unavailable_support_do_not_invent_geometry():
    baseline = _edge()
    identical = _geometry(baseline, baseline, 0, 0)
    assert identical['measured_roi_center_displacement_px'] == 0
    assert identical['measured_local_roi_skew_px'] == 0
    unsupported = _geometry(baseline, baseline, valid=[8, 248, 100, 156])
    assert unsupported['status'] == 'unavailable'
    assert unsupported['measured_roi_center_displacement_px'] is None
    assert unsupported['measured_active_array_skew_extrapolated_px'] is None


def test_new_optional_search_parameter_preserves_every_default_legacy_result():
    a, b = _edge(shift=-.5), _edge()
    kwargs = dict(valid_rect=[8, 248, 49, 207], edge_roi=[108, 148, 108, 148], theoretical_skew_px=0)
    assert legacy_geometry(a, b, **kwargs) == legacy_geometry(a, b, **kwargs, search_halo_native_px=8)


@pytest.mark.parametrize('offset', [float('nan'), True, None])
def test_invalid_timing_diagnostics_are_rejected(offset):
    with pytest.raises(ValueError, match='finite center'):
        _geometry(_edge(), _edge(), offset=offset)

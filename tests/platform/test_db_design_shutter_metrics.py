"""Independent geometry, row-timing integration and bounded shutter choices."""
import copy

import numpy as np
import pytest
from scipy.special import erf

from camera_platform import db_design_motion as global_motion
from camera_platform import db_design_shutter as engine
from camera_platform import db_design_shutter_metrics as metrics
from test_db_design_motion_metrics import _joint


def _edge(shape=(256, 256), slope=.08, shift=0):
    y, x = np.indices(shape, dtype=float)
    center = (shape[1] - 1) / 2 + slope * (y - (shape[0] - 1) / 2) + shift
    values = 10 + 100 * (1 + erf((x - center) / 3)) / 2
    return values[..., None] * np.array([.6, 1, .9, .4])


def _geometry(rs, gs, theory=0, **kwargs):
    return metrics.geometry_from_planes(
        rs, gs, valid_rect=kwargs.get('valid_rect', [8, 248, 49, 207]),
        edge_roi=kwargs.get('edge_roi', [108, 148, 108, 148]), theoretical_skew_px=theory,
    )


@pytest.mark.parametrize('exposure', [1, 4, 16])
def test_measured_geometry_is_paired_to_same_exposure_image_not_theory(exposure):
    original = _edge()
    velocity, readout = 1705.7158898726516, 30
    gs = engine.integrate_row_planes(original, velocity, exposure, 0)
    rs = engine.integrate_row_planes(original, velocity, exposure, readout)
    expected = velocity * readout / 1000
    result = _geometry(rs, gs, expected)
    assert result['status'] == 'ok'
    assert result['measured_full_probe_skew_px'] == pytest.approx(expected, abs=.015)
    assert result['observed_row_span_px'] == 39
    assert result['measured_observed_skew_px'] == pytest.approx(result['relative_slope_dx_dy'] * 39)
    assert result['measured_full_probe_skew_px'] == pytest.approx(result['relative_slope_dx_dy'] * 255)
    assert result['fit_rmse_px'] < .01 and result['fit_r_squared'] > .9999
    assert result['relative_tilt_deg'] > 0
    misleading_theory = _geometry(rs, gs, 9999)
    for key in ('measured_full_probe_skew_px', 'measured_observed_skew_px',
                'rs_edge_x_px', 'gs_edge_x_px', 'relative_tilt_deg'):
        assert misleading_theory[key] == result[key]


def test_geometry_scale_invariant_and_existing_chart_tilt_cancels():
    gs = _edge(slope=.12)
    rs = _edge(slope=.21)
    result = _geometry(rs, gs, .09 * 255)
    scaled = _geometry(rs * .001, gs * .001, .09 * 255)
    assert result['measured_full_probe_skew_px'] == pytest.approx(.09 * 255, abs=.01)
    assert scaled['measured_full_probe_skew_px'] == pytest.approx(result['measured_full_probe_skew_px'], abs=1e-10)
    assert result['gs_edge_tilt_deg'] > 6
    identical = _geometry(gs, gs)
    assert identical['measured_full_probe_skew_px'] == 0
    assert identical['fit_rmse_px'] == 0 and identical['fit_r_squared'] == 1


def test_reverse_shear_is_measured_with_its_actual_sign():
    result = _geometry(_edge(slope=-.05), _edge(slope=.08), -.13 * 255)
    assert result['measured_full_probe_skew_px'] == pytest.approx(-.13 * 255, abs=.01)
    assert result['relative_tilt_deg'] < 0


@pytest.mark.parametrize('kind', ['flat', 'outside_support', 'missing_roi', 'too_few_rows'])
def test_unavailable_geometry_is_never_reported_as_zero(kind):
    gs = _edge()
    args = {}
    if kind == 'flat':
        gs[:] = 1
    elif kind == 'outside_support':
        args['valid_rect'] = [8, 248, 110, 207]
    elif kind == 'missing_roi':
        args['edge_roi'] = None
    else:
        args['edge_roi'] = [120, 125, 108, 148]
    result = _geometry(gs, gs, **args)
    assert result['status'] == 'unavailable' and result['reason']
    assert result['measured_full_probe_skew_px'] is None


@pytest.mark.parametrize('corruption', ['shape', 'negative', 'nan'])
def test_invalid_plane_contract_is_rejected(corruption):
    a, b = _edge(), _edge()
    if corruption == 'shape':
        a = a[..., :3]
    elif corruption == 'negative':
        a[0, 0, 0] = -1
    else:
        a[0, 0, 0] = np.nan
    with pytest.raises(ValueError, match='matching finite nonnegative'):
        _geometry(a, b)


def _row(identity, exposure, readout, *, sfr=.15):
    return {**_joint(identity, 4.12, exposure, sfrs=(sfr, sfr)), 'readout_ms': readout,
            'shutter_mode': 'global' if readout == 0 else 'rolling'}


def test_selection_does_not_call_high_sfr_rolling_a_better_readout():
    rows = [_row('gs1', 1, 0), _row('gs4', 4, 0, sfr=.2),
            _row('rs1', 1, 30, sfr=.4), _row('rs4', 4, 30, sfr=.3)]
    before = copy.deepcopy(rows)
    result = metrics.assess(rows)
    assert result['recommended_candidate_id'] == 'gs4'
    assert result['eligible_candidate_ids'] == ['gs4', 'gs1']
    assert result['selections_by_readout_ms']['30']['recommended_candidate_id'] == 'rs1'
    assert {item['candidate_id'] for item in result['decisions']} == {row['candidate_id'] for row in rows}
    assert result['cross_readout_ranking'] is False
    assert result['selection_basis'] == 'within_readout_only'
    assert rows == before


def test_fractional_readout_groups_preserve_float_identity_and_every_decision():
    rows = [_row('gs', 1, 0), _row('tiny', 1, 1e-7),
            _row('near_a', 1, 5.000001), _row('near_b', 1, 5.000002)]
    result = metrics.assess(rows)
    groups = result['selections_by_readout_ms']
    assert list(groups) == ['0', '1e-07', '5.000001', '5.000002']
    assert [group['readout_ms'] for group in groups.values()] == [0, 1e-7, 5.000001, 5.000002]
    assert [group['recommended_candidate_id'] for group in groups.values()] == ['gs', 'tiny', 'near_a', 'near_b']
    assert {item['candidate_id'] for item in result['decisions']} == {row['candidate_id'] for row in rows}
    assert result['recommended_candidate_id'] == 'gs'


@pytest.mark.parametrize('invalid', ['negative', 'nan', 'mode', 'no_gs', 'duplicate_id'])
def test_invalid_shutter_groups_are_rejected(invalid):
    rows = [_row('gs', 1, 0), _row('rs', 1, 30)]
    if invalid == 'negative':
        rows[-1]['readout_ms'] = -1
    elif invalid == 'nan':
        rows[-1]['readout_ms'] = float('nan')
    elif invalid == 'mode':
        rows[-1]['shutter_mode'] = 'global'
    elif invalid == 'no_gs':
        rows.pop(0)
    else:
        rows[-1]['candidate_id'] = 'gs'
    with pytest.raises(ValueError):
        metrics.assess(rows)


def test_geometry_is_reported_separately_from_within_readout_exposure_constraints():
    rows = [_row('gs', 1, 0), _row('rs', 1, 30)]
    for field in rows[-1]['fields']:
        field['shutter_geometry'] = {'status': 'ok', 'measured_full_probe_skew_px': 50}
    result = metrics.assess(rows)
    assert result['selections_by_readout_ms']['30']['recommended_candidate_id'] == 'rs'
    assert result['recommended_candidate_id'] == 'gs'


@pytest.mark.parametrize('velocity,exposure,readout', [(350, 1, 15), (350, 4, 30), (350, 16, 5)])
def test_row_integral_matches_independent_dense_time_quadrature(velocity, exposure, readout):
    original = np.random.default_rng(42).random((64, 64, 4)) * 10
    actual = engine.integrate_row_planes(original, velocity, exposure, readout)
    for row in (0, 31, 63):
        mid = (row / 63 - .5) * readout / 1000
        times = np.linspace(mid - exposure / 2000, mid + exposure / 2000, 32769)
        for channel in range(4):
            dense = np.interp(32 - velocity * times, np.arange(64), original[row, :, channel])
            expected = np.trapezoid(dense, times) / (exposure / 1000)
            assert actual[row, 32, channel] == pytest.approx(expected, abs=1e-7)


def test_row_time_direction_and_zero_limits_preserve_full_cfa_planes():
    start, mid, end = engine.row_timing(256, 4, 30)
    assert mid[0] == -.015 and mid[-1] == .015
    assert start[0] == -.017 and end[-1] == .017
    np.testing.assert_allclose(end - start, .004, rtol=0, atol=1e-17)
    np.testing.assert_allclose(np.diff(mid), .030 / 255, rtol=0, atol=1e-17)
    assert (mid[127] + mid[128]) / 2 == pytest.approx(0, abs=1e-17)
    source = np.random.default_rng(17).random((64, 64, 4))
    assert np.array_equal(engine.integrate_row_planes(source, 0, 4, 30), source)
    assert np.array_equal(engine.integrate_row_planes(source, 350, 4, 0), global_motion.integrate_planes(source, 1.4))
    planes = np.ones((64, 64, 4)) * np.array([1, 3, 8, 13])
    integrated = engine.integrate_row_planes(planes, 350, 4, 30)
    np.testing.assert_allclose(integrated, planes, rtol=1e-14, atol=1e-14)
    mosaic = global_motion.remosaic_native(integrated)
    for index, site in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
        np.testing.assert_allclose(mosaic[site[0]::2, site[1]::2], [1, 3, 8, 13][index])

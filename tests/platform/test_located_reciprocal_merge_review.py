"""Located pair batches preserve actual radiometry and field/direction identity."""
import copy

import numpy as np
import pytest

from camera_platform import reference_workshop_optics as optics


def _arrays(height):
    rays = 4
    return {
        'irradiance_per_radiance_sr': np.array([[.1, height], [.2, height * 2]]),
        'directional_conditional_weights': np.array([
            [[.1, .2, .3, .4], [height, 0, 0, 1 - height]],
            [[.4, .3, .2, .1], [0, height, 1 - height, 0]],
        ]),
        'image_direction_cosines': np.array([[[0., 0., 1.]] * rays, [[height, 0., np.sqrt(1 - height ** 2)]] * rays]),
        'theta_deg': np.arange(rays + int(height * 10)),
        'angular_weights': np.ones((2, 2, rays + int(height * 10))),
    }


def test_pair_partition_preserves_field_and_wavelength_axes_without_histogram_merge(tmp_path, monkeypatch):
    requests = []
    request = {'wavelength_nm': [450., 460.], 'image_height_mm': [0., .1, .2],
               'field_radius': [0., .5, 1.], 'sampling': {'polar_rings': 28, 'azimuth_samples': 48}}
    original = copy.deepcopy(request)

    def batch(directory, label, part, backend, execute, progress):
        requests.append(copy.deepcopy(part))
        return {'status': 'PROPOSED'}, _arrays(part['image_height_mm'][1])

    monkeypatch.setattr(optics.shared, '_batch', batch)
    summaries, combined = optics._located_reciprocal(tmp_path, request, {}, None, lambda _: None)
    assert request == original
    assert [r['image_height_mm'] for r in requests] == [[0., .1], [0., .2]]
    assert all(r['wavelength_nm'] == [450., 460.] and r['field_radius'] == [0., 1.] for r in requests)
    assert all(r['sampling'] == original['sampling'] for r in requests)
    assert len(summaries) == 2 and all(s['status'] == 'PROPOSED' for s in summaries)
    assert set(combined) == {'irradiance_per_radiance_sr', 'directional_conditional_weights', 'image_direction_cosines'}
    np.testing.assert_array_equal(combined['irradiance_per_radiance_sr'], [[.1, .1, .2], [.2, .2, .4]])
    for position, height in [(1, .1), (2, .2)]:
        np.testing.assert_array_equal(combined['directional_conditional_weights'][:, position], _arrays(height)['directional_conditional_weights'][:, 1])
        np.testing.assert_array_equal(combined['image_direction_cosines'][position], _arrays(height)['image_direction_cosines'][1])


@pytest.mark.parametrize('name', ['irradiance_per_radiance_sr', 'directional_conditional_weights', 'image_direction_cosines'])
def test_on_axis_disagreement_is_rejected_instead_of_silently_renormalized(tmp_path, monkeypatch, name):
    def batch(directory, label, part, backend, execute, progress):
        height = part['image_height_mm'][1]
        values = _arrays(height)
        if height == .2:
            if name == 'irradiance_per_radiance_sr':
                values[name][0, 0] += .01
            elif name == 'directional_conditional_weights':
                values[name][0, 0, 0] += .01
            else:
                values[name][0, 0, 0] += .01
        return {'status': 'PROPOSED'}, values

    monkeypatch.setattr(optics.shared, '_batch', batch)
    with pytest.raises(ValueError, match='common on-axis ' + name):
        optics._located_reciprocal(tmp_path, {'image_height_mm': [0., .1, .2]}, {}, None, lambda _: None)

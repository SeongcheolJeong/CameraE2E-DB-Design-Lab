import numpy as np
from camera_platform import lens_factory as factory,lens_psf_lut_radiometry as radial


def test_companded_disk_keeps_full_area_and_matches_analytic_on_axis_flux():
    data=factory.validate_request(factory.request_template())
    data['sampling'].update(polar_rings=128,azimuth_samples=64)
    data['image_height_mm']=[0.,.002]
    original=factory._sampling
    xy,direction,weights,distance=radial.sampling(data,1)
    assert factory._sampling is original
    radius=data['surfaces'][-1]['semi_diameter_mm']
    area=weights/(direction[:,:,2]**4/distance**2)
    np.testing.assert_allclose(area.sum(axis=1),np.pi*radius**2,rtol=1e-12)
    exact=np.pi*radius**2/(distance**2+radius**2)
    np.testing.assert_allclose(weights[0].sum(),exact,rtol=1e-7)
    assert np.max(np.linalg.norm(xy[1],axis=-1))>.98*radius
    # Both subpixel-near-axis and true-axis quadrature see the same smooth
    # clear-disk radiometry without dropping the outer annuli.
    np.testing.assert_allclose(weights[1].sum(),exact,rtol=1e-3)

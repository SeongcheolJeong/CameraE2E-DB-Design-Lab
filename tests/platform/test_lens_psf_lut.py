"""Physical units, aperture separation, conservative coverage and interpolation."""
import copy
import math

import numpy as np
import pytest
from scipy.special import j1

from camera_platform import lens_psf_lut as lut


def test_specification_keeps_sensor_out_of_optical_identity():
    s=lut.specification({})
    assert 'pixel_pitch_um' not in s and s['optical_sample_um']==.5
    with pytest.raises(ValueError):lut.specification(dict(pixel_pitch_um=2.9))
    with pytest.raises(ValueError):lut.specification(dict(initial_grid_xy=[10,10],max_field_nodes=100))
    with pytest.raises(ValueError):lut.specification(dict(wavelength_nm=[650,450]))


def test_point_sampled_clear_pupil_matches_airy_without_pixel_box():
    n=192; dq=.00125; q=(np.arange(n)-n//2)*dq
    xx,yy=np.meshgrid(q,q); aperture=(xx**2+yy**2 <= .09**2).astype(float)
    p=dict(power=aperture,opd_nm=np.zeros_like(aperture),spacing=dq)
    spacing=.5;size=129;wave=550.
    density=lut.intensity(p,wave,size,spacing)
    x=(np.arange(size)-size//2)*spacing
    z=2*np.pi*.09*np.abs(x)/(.55)
    airy=np.ones_like(z);mask=z!=0;airy[mask]=(2*j1(z[mask])/z[mask])**2
    actual=density[size//2]/density[size//2,size//2]
    assert np.max(np.abs(actual-airy))<.006
    assert .95 < density.sum()*spacing**2 < 1.001
    with pytest.raises(ValueError):lut.intensity(p,wave,size,2.)


def test_pixel_rectangle_integral_conserves_mass_and_has_expected_area():
    density=np.ones((21,21))/441
    kernel,meta=lut.integrate_pixel(density,1.,2.,11)
    assert kernel.sum()==pytest.approx(1)
    assert kernel[5,5]==pytest.approx(4/441)
    assert kernel[0,0]==pytest.approx(2.25/441)
    assert meta['pixel_aperture_applications']==1
    assert meta['normalization']=='physical_bin_mass_not_renormalized'


def test_same_optical_lut_supports_two_sensor_pitches_and_fractional_position():
    axis=(np.arange(81)-40)*.25;xx,yy=np.meshgrid(axis,axis)
    a=np.exp(-(xx**2+yy**2)/2);a/=a.sum()*.25**2
    one,_=lut.integrate_pixel(a,.25,1.,41)
    two,_=lut.integrate_pixel(a,.25,2.,21)
    shifted,_=lut.integrate_pixel(a,.25,1.,41,(.5,0))
    assert one.sum()==pytest.approx(two.sum())
    assert two[10,10]>one[20,20]
    assert abs((shifted.sum(0)*np.arange(41)).sum()-20-.5)<.001
    with pytest.raises(ValueError):lut.integrate_pixel(a,.25,.3,41)
    with pytest.raises(ValueError):lut.integrate_pixel(a,.25,1.,3)


def evaluator(spec,nonlinear=False):
    def run(uv):
        x,y=uv
        weight=.2+.6*((x/np.tan(np.radians(spec['half_field_deg_xy'][0])))**2 if nonlinear else .5+x)
        a=np.zeros((1,spec['optical_size'],spec['optical_size']),np.float32)
        c=spec['optical_size']//2
        a[0,c,c]=weight/spec['optical_sample_um']**2
        a[0,c,c+2]=(1-weight)/spec['optical_sample_um']**2
        return a,dict(chief_sensor_xy_mm=[x,y])
    return run


def small_spec(**changes):
    return lut.specification(dict(wavelength_nm=[550.],optical_size=65,optical_sample_um=.5,
         initial_grid_xy=[2,2],max_field_nodes=81,half_field_deg_xy=[1.,1.],**changes))


def test_adaptive_linear_field_is_qualified_by_actual_off_corner_samples():
    s=small_spec();nodes,a,cells=lut.adaptive_grid(s,evaluator(s))
    assert len(nodes)==9 and len(cells)==1
    assert cells[0]['status']=='qualified' and len(cells[0]['probes'])==5
    d=dict(specification=s,nodes=nodes,cells=cells)
    p,m=lut.interpolate(d,a,[.003,.004],550.)
    expected=evaluator(s)((.003,.004))[0][0]
    assert np.max(np.abs(p-expected))<1e-6
    assert m['method']=='qualified_bilinear'
    with pytest.raises(ValueError):lut.interpolate(d,a,[.1,0],550.)
    with pytest.raises(ValueError):lut.interpolate(d,a,[0,0],560.)


def test_adaptive_nonlinear_edges_refine_and_exhaustion_is_not_success():
    s=small_spec(interpolation_l1_limit=.35,max_refinement_level=2)
    nodes,a,cells=lut.adaptive_grid(s,evaluator(s,True))
    assert len(nodes)>9 and any(c['level']>=1 for c in cells)
    assert all(c['tolerance']==pytest.approx(.175 if c['outer'] else .35) for c in cells)
    assert any(c['status']=='qualified' for c in cells)
    s=small_spec(interpolation_l1_limit=.001,max_refinement_level=0)
    nodes,a,cells=lut.adaptive_grid(s,evaluator(s,True))
    assert cells[0]['status']=='unresolved'
    with pytest.raises(ValueError):lut.interpolate(dict(specification=s,nodes=nodes,cells=cells),a,[.003,.004],550.)
    # An exact physically evaluated node remains available without claiming
    # interpolation accuracy for the unresolved area around it.
    _,meta=lut.interpolate(dict(specification=s,nodes=nodes,cells=cells),a,[0,0],550.)
    assert meta['method']=='exact_node'


def test_optical_convolution_preserves_uniform_interior_and_impulse_mass():
    kernel=np.ones((3,3))/9/.25
    image=np.ones((33,33))
    out=lut.apply_local_radiance(image,kernel,.5)
    assert np.max(np.abs(out[2:-2,2:-2]-1))<1e-12
    impulse=np.zeros((33,33));impulse[16,16]=1
    assert lut.apply_local_radiance(impulse,kernel,.5).sum()==pytest.approx(1)


def test_refused_physical_node_is_a_coverage_hole_never_a_zero_psf():
    s=small_spec(max_refinement_level=1)
    good=evaluator(s)
    def solve(uv):
        if uv[0]>0 and uv[1]>0:
            return np.zeros((1,65,65)),dict(status='refused',reason='unsupported physical pupil')
        return good(uv)
    nodes,a,cells=lut.adaptive_grid(s,solve)
    assert any(n.get('status')=='refused' for n in nodes)
    assert any(c['status']=='unresolved' for c in cells)
    assert any(c['status']=='qualified' for c in cells)
    bad=next(n for n in nodes if n.get('status')=='refused')
    with pytest.raises(ValueError,match='refused'):
        lut.interpolate(dict(specification=s,nodes=nodes,cells=cells),a,bad['uv'],550.)

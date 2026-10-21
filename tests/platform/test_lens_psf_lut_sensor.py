import copy
import numpy as np
import pytest

from camera_platform.lens_psf_lut_sensor import evaluate_local_sensor_rate


def inputs():
    spec=dict(wavelength_nm=[500.,600.],half_field_deg_xy=[1.,1.],optical_sample_um=.5,optical_size=65)
    document=dict(specification=spec,lut_id='test-lut',nodes=[dict(id=0,uv=[0.,0.])],cells=[],
          pinned_source=dict(lens_source=dict(revision_id='patent-1',applied_prescription_sha256='physical-iris')))
    d=np.zeros((1,2,65,65));d[0,:,32,32]=4
    args=dict(uv=[0.,0.],pixel_pitch_um=2.,output_shape_row_col=[4,4],
          radiometry=dict(lens_revision_id='patent-1',applied_prescription_sha256='physical-iris',
                     wavelength_nm=[500.,600.],irradiance_per_radiance_sr=[.1,.2]),
          absolute_qe=[[.1,.2,.3,.4],[.2,.3,.4,.5]],spectral_bin_width_nm=[10.,20.])
    photons=np.ones((81,81,2))*1e15
    return document,d,photons,args


def test_physical_electron_rate_area_spectrum_and_cfa_are_applied_once():
    doc,d,p,args=inputs();r=evaluate_local_sensor_rate(doc,d,p,**args)
    expected=1e15*4e-12*(.1*10*np.array([.1,.2,.3,.4])+.2*20*np.array([.2,.3,.4,.5]))
    assert np.max(np.abs(r['planes_electron_rate']-expected))<1e-9
    assert np.allclose(r['mean_electron_rate'][:2,:2],expected.reshape(2,2))
    assert r['metadata']['ray_tracing_calls']==0 and r['metadata']['pixel_aperture_applications']==1
    args['cfa_origin_row_col']=[1,1]
    shifted=evaluate_local_sensor_rate(doc,d,p,**args)
    assert shifted['mean_electron_rate'][0,0]==pytest.approx(expected[3])


def test_larger_receiving_area_changes_signal_without_retracing_lens():
    doc,d,p,args=inputs();one=evaluate_local_sensor_rate(doc,d,p,**args)
    # Keep the declared scene grid and a smaller output to retain its halo.
    args.update(pixel_pitch_um=4.,output_shape_row_col=[2,2])
    two=evaluate_local_sensor_rate(doc,d,p,**args)
    assert np.allclose(two['planes_electron_rate'],4*one['planes_electron_rate'][0,0])


def test_missing_halo_or_wrong_iris_cannot_use_lut_as_absolute_throughput():
    doc,d,p,args=inputs()
    with pytest.raises(ValueError):evaluate_local_sensor_rate(doc,d,p[2:-2,2:-2],**args)
    args['radiometry']['applied_prescription_sha256']='other-iris'
    with pytest.raises(ValueError):evaluate_local_sensor_rate(doc,d,p,**args)

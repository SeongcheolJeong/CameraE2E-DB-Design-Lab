"""Row-time integration is causal geometry, not a post-RAW image transform."""

import copy
import json

import numpy as np
import pytest
from scipy.ndimage import map_coordinates

from camera_platform import db_design_shutter as shutter
from camera_platform import db_design_motion as motion
from camera_platform.virtual_lab_job import sha256
from test_db_design_motion import _payload
from test_db_design_conditions import _fake_render as static_render


def test_row_timing_is_first_last_probe_scan_centered_at_common_frame_time():
    starts, mids, ends=shutter.row_timing(256,4,30)
    assert mids[0]==-.015 and mids[-1]==.015
    np.testing.assert_allclose(ends-starts,.004,atol=2e-18)
    assert mids[127] == pytest.approx(-mids[128])
    np.testing.assert_allclose(np.diff(mids),.03/255,atol=3e-18)
    projection=motion.source_projection([_payload()])
    metadata=shutter.shutter_metadata(projection,4,20,4,30,[8,248,49,207])
    assert metadata['top_bottom_skew_px']==pytest.approx(51.17147669617955)
    assert metadata['blur_length_px']==pytest.approx(6.822863559490607)
    assert metadata['row_timing_summary']['center']['mid_s']==0
    assert metadata['row_timing_summary']['center']['row_index']==127.5
    assert metadata['native_probe_rows']==256


@pytest.mark.parametrize('velocity,exposure,readout',[(100,1,5),(1705.7,4,30),(1705.7,16,30),(1705.7,4,0)])
def test_exact_row_integral_matches_independent_time_sampling(velocity,exposure,readout):
    rng=np.random.default_rng(171)
    values=rng.uniform(0,100,(16,96,4))
    actual=shutter.integrate_row_planes(values,velocity,exposure,readout)
    starts,mids,ends=shutter.row_timing(16,exposure,readout)
    columns=np.arange(40,56)
    for row in [0,7,8,15]:
        times=np.linspace(starts[row],ends[row],16385)
        positions=columns[None,:]-velocity*times[:,None]
        for channel in range(4):
            samples=map_coordinates(values[row,:,channel],[positions],order=1,mode='nearest')
            expected=np.trapezoid(samples,x=times,axis=0)/(exposure/1000)
            np.testing.assert_allclose(actual[row,columns,channel],expected,atol=1e-5,rtol=0)
    _,kernel=shutter.row_kernel(16,velocity,exposure,readout)
    np.testing.assert_allclose(kernel.sum(axis=1),1,atol=1e-12)
    assert np.all(kernel>=0)


def test_zero_speed_and_global_shutter_retain_existing_exact_values():
    values=np.random.default_rng(21).uniform(0,1e6,(16,64,4))
    for readout in [0,5,15,30]:
        np.testing.assert_array_equal(shutter.integrate_row_planes(values,0,4,readout),values)
    for velocity in [0,100,1705.7]:
        for exposure in [1,4,16]:
            np.testing.assert_array_equal(shutter.integrate_row_planes(values,velocity,exposure,0),
                motion.integrate_planes(values,velocity*exposure/1000))


def test_rolling_operator_has_correct_horizontal_direction_and_cfa_isolation():
    columns=np.arange(128,dtype=float)
    values=np.zeros((16,128,4))
    values[:,:,0]=columns+100
    actual=shutter.integrate_row_planes(values,1000,4,20)
    _,mids,_=shutter.row_timing(16,4,20)
    np.testing.assert_allclose(actual[:,32:96,0],100+columns[None,32:96]-1000*mids[:,None],atol=1e-12)
    assert not np.any(actual[:,:,1:])
    mosaic=motion.remosaic_native(actual)
    assert not np.any(mosaic[1::2,:]) and not np.any(mosaic[:,1::2])
    assert actual[0,64,0]>values[0,64,0]>actual[-1,64,0]


def test_one_integral_is_not_blur_then_fractional_resample():
    values=np.zeros((16,128,4)); values[:,64,:]=100
    actual=shutter.integrate_row_planes(values,100,4,5)
    blurred=motion.integrate_planes(values,.4)
    _,mids,_=shutter.row_timing(16,4,5)
    wrong=np.empty_like(values)
    for row in range(16):
        for channel in range(4):
            wrong[row,:,channel]=map_coordinates(blurred[row,:,channel],
                [np.arange(128)-100*mids[row]],order=1,mode='nearest')
    assert np.max(abs(actual-wrong))>1


def test_global_support_includes_blur_and_probe_scan():
    velocity=motion.image_velocity_px_s(4,20,24.732880403153448,2.9)
    assert motion.common_valid_rect((256,256),velocity*(16+30)/1000)==[8,248,49,207]
    with pytest.raises(ValueError,match='fixed central SFR'):
        motion.common_valid_rect((256,256),velocity*(16+120)/1000)


def test_zero_speed_raw_is_identical_across_readouts_and_saves_timing(tmp_path,monkeypatch):
    def render(*args):
        result=static_render(*args); result['images']={}; return result
    monkeypatch.setattr(shutter.acquisition.reference,'_render_field',render)
    monkeypatch.setattr(shutter.metrics,'geometry_from_planes',lambda actual,gs,**kw:{
        'status':'ok','theoretical_full_probe_skew_px':kw['theoretical_skew_px']})
    payload=_payload(); projection=motion.source_projection([payload])
    condition={'id':'condition-000','luminance_cd_m2':1000,'object_speed_m_s':0,
               'common_valid_rect_row_col':[8,248,49,207]}
    identity={'run_id':'source','manifest_sha256':'a'*64}
    gs=shutter._camera(tmp_path,payload,condition,projection,20,4,1,0,0,4.12,identity,lambda _:None,lambda:None)
    rs=shutter._camera(tmp_path,payload,condition,projection,20,4,1,30,1,4.12,identity,lambda _:None,lambda:None)
    for field in shutter.acquisition.FIELDS:
        for chart in shutter.acquisition.CHARTS:
            for repeat in range(8):
                path=f'fields/{field}/{chart}/raw-{repeat:02d}.npy'
                assert sha256(tmp_path/gs['prefix']/path)==sha256(tmp_path/rs['prefix']/path)
    metadata=json.loads((tmp_path/rs['prefix']/'row-timing.json').read_text())
    assert metadata['row_exposure_mid_s'][0]==-.015
    assert metadata['row_exposure_mid_s'][-1]==.015
    assert metadata['line_time_ms']==pytest.approx(30/255)
    assert rs['motion']['row_timing_sha256']==sha256(tmp_path/rs['prefix']/'row-timing.json')
    for field in rs['result']['fields']:
        diagnostic=field['shutter_geometry']
        assert diagnostic['global_reference_sha256']==sha256(tmp_path/rs['prefix']/'fields'/field['id']/diagnostic['global_reference_file'])


def test_run_selects_source_subset_and_keeps_global_support(tmp_path,monkeypatch):
    source=tmp_path/'source'; source.mkdir()
    (source/'manifest.json').write_text(json.dumps({'schema':'camera_reference_workshop_files_v1','files':{}}))
    monkeypatch.setattr(shutter.acquisition,'_validate_source',lambda reader,summary:[_payload(1),_payload(3)])
    seen=[]
    def camera(root,payload,cell,projection,depth,exposure,ei,readout,ri,f,identity,notify,check):
        cid=f"{payload['candidate']['id']}-e{ei:02d}-r{ri:02d}"
        prefix=f"conditions/{cell['id']}/candidates/{cid}/"; (root/prefix).mkdir(parents=True)
        seen.append((f,copy.deepcopy(cell),readout))
        return {'id':cid,'prefix':prefix,'result':{'fields':[]}}
    monkeypatch.setattr(shutter,'_camera',camera)
    monkeypatch.setattr(shutter,'comparison',lambda root,candidates,*args:{'rows':[{'fields':[]} for _ in candidates]})
    monkeypatch.setattr(shutter,'compact_candidate',lambda c,d:{**c,'detail_sha256':d})
    request={'source_run_id':'source','f_numbers':[4.12],'luminance_cd_m2':[1000],
        'object_speed_m_s':[0,4],'object_distance_m':20,'exposure_ms':[1,4,16],
        'readout_ms':[0,30],'selection_policy':{}}
    checks=[]
    result=shutter.run(None,tmp_path/'out',request,source,{'id':'source','configuration':{'f_numbers':[2.75,4.12]}},
        lambda _:None,check=lambda:checks.append(1))
    assert len(seen)==12 and len(checks)==3
    assert all(f==4.12 and cell['common_valid_rect_row_col']==[8,248,49,207] for f,cell,rd in seen)
    assert result['default_candidate_a_id']=='candidate-03-e01-r00'
    assert result['default_candidate_b_id']=='candidate-03-e01-r01'
    assert result['aperture_conditions']['default_condition_id']=='condition-001'
    assert result['capture_count']==12*64

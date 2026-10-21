"""Located sensor rows preserve the common clock and use fresh physical fields."""
import copy
import json
from pathlib import Path

import numpy as np
import pytest
from scipy.ndimage import map_coordinates

from camera_platform import db_design_sensor_shutter as sensor
from camera_platform import db_design_sensor_timing as timing
from camera_platform import db_design_motion as motion
from camera_platform import db_design_optics as optics
from camera_platform import reference_workshop_optics as shared_optics
from test_db_design_motion import _payload
from test_db_design_conditions import _fake_render

ROOT=Path(__file__).resolve().parents[2]


def test_actual_chief_points_match_even_located_active_roi_centers():
    profile=timing.profile(ROOT)
    context=optics._source('canon_p0248')
    spec=optics.template('canon_p0248',optical_profile='exit_pupil_diffraction')
    fields=sensor.located_fields(ROOT,spec,context,profile)
    assert fields[0]['target_image_xy_mm']==[0.,0.]
    assert fields[0]['sensor_placement']['roi_center_image_xy_mm']==[-.00145,.00145]
    assert fields[0]['sensor_placement']['optical_anchor_local_row_col']==[128.,128.]
    assert fields[1]['target_image_xy_mm']==[2.24895,1.03385]
    assert fields[1]['object_angle_deg']==pytest.approx(5.73048011624,abs=1e-8)
    assert fields[1]['target_image_xy_mm']!=[5.6447450176,5.6447450176]
    for f in fields:
        start=f['sensor_placement']['roi_origin_active_row_col']
        assert all(x%2==0 for x in start)
        assert all(0<=x and x+256<=n for x,n in zip(start,[1097,1937]))
    # A located optical request is a distinct immutable cache identity.
    old=shared_optics.cache_key(ROOT,spec,source_context=context)
    new=shared_optics.cache_key(ROOT,spec,source_context={**context,'located_fields':fields})
    assert old!=new


@pytest.mark.parametrize('field_id',['center','outer-ne'])
@pytest.mark.parametrize('exposure',[1,4,16])
def test_absolute_row_integral_matches_direct_temporal_quadrature(field_id,exposure):
    profile=timing.profile(ROOT)
    clock=timing.field_timing(profile,field_id,20,exposure)
    values=np.random.default_rng(32).uniform(0,100,(256,128,4))
    velocity=1705.7158898726518
    actual=sensor.integrate_located_planes(values,velocity,exposure,clock)
    columns=np.arange(45,80)
    for row in [0,127,255]:
        times=np.linspace(clock['row_exposure_start_s'][row],clock['row_exposure_end_s'][row],16385)
        samples=map_coordinates(values[row,:,1],[columns[None,:]-velocity*times[:,None]],order=1,mode='nearest')
        expected=np.trapezoid(samples,x=times,axis=0)/(exposure/1000)
        np.testing.assert_allclose(actual[row,columns,1],expected,rtol=0,atol=1e-5)


def test_global_and_stationary_identity_preserves_old_operator():
    profile=timing.profile(ROOT)
    values=np.random.default_rng(55).uniform(0,100,(256,128,4))
    for fid in ['center','outer-ne']:
        clock=timing.field_timing(profile,fid,0,4)
        np.testing.assert_array_equal(sensor.integrate_located_planes(values,1700,4,clock),motion.integrate_planes(values,6.8))
        clock=timing.field_timing(profile,fid,20,4)
        np.testing.assert_array_equal(sensor.integrate_located_planes(values,0,4,clock),values)


def test_two_roi_times_are_not_recentered_and_total_active_local_differ():
    profile=timing.profile(ROOT);projection=motion.source_projection([_payload()])
    center=sensor.field_metadata(profile,'center',20,4,projection,4,20,[8,248,40,216])
    corner=sensor.field_metadata(profile,'outer-ne',20,4,projection,4,20,[8,248,40,216])
    assert center['roi_center_time_s']==pytest.approx(-.00001)
    assert corner['roi_center_time_s']==pytest.approx(-.00713)
    assert corner['roi_center_displacement_px']==pytest.approx(-12.161754294792007)
    assert corner['local_roi_scan_ms']==pytest.approx(5.1)
    assert corner['active_scan_ms']==pytest.approx(21.92)
    assert corner['full_sensor_scan_ms']==pytest.approx(22.16)
    assert corner['row_timing_summary']['first']['active_row']==64
    assert corner['row_timing_summary']['last']['active_row']==319
    assert center['reference_active_row']==corner['reference_active_row']==548


def test_capture_records_distinct_absolute_roi_times_without_source_mutation(tmp_path,monkeypatch):
    def render(*args):
        data=_fake_render(*args);data['images']={};return data
    monkeypatch.setattr(sensor.acquisition.reference,'_render_field',render)
    monkeypatch.setattr(sensor.metrics,'geometry_from_planes',lambda a,b,**kw:{'status':'ok',**kw})
    payload=_payload();payload['generated_source_prefix']='located-source/candidate-01/'
    payload['optical-response']['revision']='fresh-located-revision'
    payload['candidate']['located_source']={'scope':'test fresh source'}
    original=copy.deepcopy(payload['candidate']['result']['configuration'])
    profile=timing.profile(ROOT);projection=motion.source_projection([payload])
    condition={'id':'condition-000','object_speed_m_s':0,'object_distance_m':20,
        'luminance_cd_m2':1000,'common_valid_rect_row_col':[8,248,40,216]}
    result=sensor._camera(tmp_path,payload,condition,projection,profile,4,1,20,2,4.12,
        {'run_id':'source','manifest_sha256':'a'*64},lambda _:None,lambda:None)
    assert result['readout_ms']==pytest.approx(22.16)
    assert result['line_time_us']==20
    for fid,mid in [('center',-.00001),('outer-ne',-.00713)]:
        cap=json.loads((tmp_path/result['prefix']/'fields'/fid/'capture.json').read_text())
        assert cap['measurement_support']['motion']['roi_center_time_s']==pytest.approx(mid)
        provenance=cap['captures']['resolution']['condition_rate_source']
        assert provenance['parent_run_id']=='source'
        assert provenance['generated_source_prefix']=='located-source/candidate-01/'
        assert provenance['generated_optical_revision']=='fresh-located-revision'
        assert provenance['origin'].endswith('not_original_virtual_field_rate')
    assert payload['candidate']['result']['configuration']==original


def test_current_lens_mismatch_is_rejected_before_recompute(tmp_path,monkeypatch):
    payload=_payload();payload['candidate']['lens_key']='canon_p0248'
    monkeypatch.setattr(sensor.db_optics,'source_metadata',lambda *a,**k:{'revision_id':'changed'})
    with pytest.raises(ValueError,match='bound source at revision_id'):
        sensor.prepare_located_source(ROOT,tmp_path,payload,timing.profile(ROOT),lambda _:None)
    assert not list(tmp_path.rglob('*.npy'))


def test_located_reciprocal_partitions_keep_precision_and_physical_node_order(tmp_path,monkeypatch):
    context=optics._source('canon_p0248')
    request=shared_optics._request(ROOT,optics.template('canon_p0248'),[450.,460.],source_context=context)['prescription']
    request.update(image_height_mm=[0.,.002,2.475],field_radius=[0.,.002/2.475,1.])
    request['sampling'].update(context['reciprocal_sampling'])
    with pytest.raises(ValueError,match='Coarse'):
        shared_optics.lens_factory.validate_request(request)
    calls=[]
    def batch(directory,name,part,backend,run,progress):
        shared_optics.lens_factory.validate_request(part)
        calls.append(copy.deepcopy(part))
        value=part['image_height_mm'][1]
        dirs=np.zeros((2,4,3));dirs[...,2]=1
        dirs[1,:,0]=value
        return {'status':'PROPOSED'},{'irradiance_per_radiance_sr':np.array([[1.,value],[2.,value*2]]),
            'directional_conditional_weights':np.full((2,2,4),.25),'image_direction_cosines':dirs}
    monkeypatch.setattr(shared_optics.shared,'_batch',batch)
    before=copy.deepcopy(request)
    summaries,arrays=shared_optics._located_reciprocal(tmp_path,request,{},None,lambda _:None)
    assert request==before and len(summaries)==2
    assert [p['image_height_mm'] for p in calls]==[[0.,.002],[0.,2.475]]
    assert all(p['wavelength_nm']==[450.,460.] and p['sampling']==request['sampling'] for p in calls)
    np.testing.assert_array_equal(arrays['irradiance_per_radiance_sr'],[[1,.002,2.475],[2,.004,4.95]])
    np.testing.assert_array_equal(arrays['image_direction_cosines'][:,0,0],[0,.002,2.475])
    assert arrays['directional_conditional_weights'].shape==(2,3,4)


def test_located_reciprocal_rejects_inconsistent_shared_axis(tmp_path,monkeypatch):
    def batch(directory,name,part,backend,run,progress):
        h=part['image_height_mm'][1]
        return {'status':'PROPOSED'},{'irradiance_per_radiance_sr':np.full((2,2),h),
            'directional_conditional_weights':np.full((2,2,4),.25),
            'image_direction_cosines':np.tile([[[0.,0.,1.]]],(2,4,1))}
    monkeypatch.setattr(shared_optics.shared,'_batch',batch)
    with pytest.raises(ValueError,match='common on-axis'):
        shared_optics._located_reciprocal(tmp_path,{'image_height_mm':[0,.002,2.475]}, {},None,lambda _:None)

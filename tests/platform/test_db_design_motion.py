"""The motion bridge integrates full rates, preserving CFA, support and exposure."""

import copy
import json
from pathlib import Path

import numpy as np
import pytest
from scipy.ndimage import map_coordinates

from camera_platform import db_design_motion as motion
from camera_platform.virtual_lab_job import sha256
from test_db_design_conditions import _payload as static_payload, _fake_render as static_render


def _payload(index=1):
    payload = static_payload(index)
    payload['pixel-cell']['pixel_pitch_um'] = 2.9
    payload['candidate']['source_identities']['lens']['source']['paraxial'].update(
        effective_focal_length_mm=24.732880403153448, wavelength_nm=550)
    payload['candidate']['result']['fields'] = []
    for field_id, field in payload['fields'].items():
        field['capture']['geometry']['pixel_pitch_um'] = 2.9
        payload['candidate']['result']['fields'].append({
            'id': field_id, 'tests': {'resolution': {'sites': {'center': {'roi': [108, 148, 108, 148]}}}}})
    return payload


@pytest.mark.parametrize('length', [0, 1e-14, 1e-8, .1, .5, 1, 2, 2.25, 9.75, 27.29145])
def test_exact_box_kernel_preserves_mass_and_center(length):
    kernel = motion.box_kernel(length)
    assert np.all(kernel >= 0)
    np.testing.assert_allclose(kernel, kernel[::-1], atol=2e-14)
    assert kernel.sum() == pytest.approx(1, abs=1e-14)
    assert len(kernel) % 2 == 1
    if 0 < length <= 2:
        np.testing.assert_array_equal(kernel, [length/8, 1-length/4, length/8])


@pytest.mark.parametrize('length', [.5, 2.25, 9.75, 27.29145])
def test_exact_integration_matches_independent_dense_temporal_quadrature(length):
    rng = np.random.default_rng(991)
    planes = rng.uniform(0, 100, (2, 64, 4))
    actual = motion.integrate_planes(planes, length)
    columns = np.arange(18, 46)
    positions = np.linspace(-length/2, length/2, 16385)
    # Evaluate the independent native linear interpolant at actual exposure times.
    coordinates = columns[None, :] - positions[:, None]
    for channel in range(4):
        sampled = map_coordinates(planes[0, :, channel], [coordinates], order=1, mode='nearest')
        expected = np.trapezoid(sampled, x=positions, axis=0) / length
        np.testing.assert_allclose(actual[0, columns, channel], expected, atol=1e-5, rtol=0)


def test_zero_motion_is_exact_and_native_channel_sites_remain_separate():
    rng = np.random.default_rng(62)
    values = rng.uniform(0, 100, (16, 24, 4))
    np.testing.assert_array_equal(motion.integrate_planes(values, 0), values)
    isolated = np.zeros((16, 24, 4)); isolated[:, :, 0] = 7
    mosaic = motion.remosaic_native(motion.integrate_planes(isolated, 5.5))
    np.testing.assert_allclose(mosaic[::2, ::2], 7)
    assert not np.any(mosaic[1::2, :])
    assert not np.any(mosaic[:, 1::2])
    # Blurring the mosaic would mix color sites; it is intentionally not this operator.
    assert mosaic[0, 1] == 0


def test_projection_and_support_retain_actual_native_units():
    payload = _payload()
    projection = motion.source_projection([payload, copy.deepcopy(payload)])
    velocity = motion.image_velocity_px_s(4, 20, 24.732880403153448, 2.9)
    assert velocity == pytest.approx(1705.7158898726518)
    assert velocity * .016 == pytest.approx(27.29145423796243)
    assert motion.common_valid_rect((256, 256), velocity * .016) == [8, 248, 23, 233]
    assert motion.common_valid_rect((256, 256), 0) == [8, 248, 8, 248]
    assert projection['focal_reference_wavelength_nm'] == 550
    payload['fields']['center']['capture']['geometry']['pixel_pitch_um'] = 3
    with pytest.raises(ValueError, match='pitch disagree'):
        motion.source_projection([payload])
    with pytest.raises(ValueError, match='fixed central SFR'):
        motion.common_valid_rect((256, 256), 200)


def test_prepare_retains_source_rates_hashes_and_rejects_wrong_cfa():
    payload = _payload()
    projection = motion.source_projection([payload])
    metadata = motion.motion_metadata(projection, 4, 20, 16, [8,248,23,233])
    original = payload['fields']['center']['arrays']['resolution']['planes-electron-rate.npy'].copy()
    prepared = motion.prepare_payload(payload, metadata, 2, 2.75)
    assert prepared['candidate']['id'] == 'candidate-01-e02'
    assert prepared['candidate']['exposure_ms'] == 16
    assert prepared['source_candidate_id'] == 'candidate-01'
    np.testing.assert_array_equal(payload['fields']['center']['arrays']['resolution']['planes-electron-rate.npy'], original)
    assert prepared['fields']['center']['capture']['captures']['resolution']['rate_sha256'] == 'source-rate'
    payload['fields']['center']['arrays']['resolution']['mean-electron-rate.npy'][0, 0] += 1
    with pytest.raises(ValueError, match='bound native mosaic'):
        motion.prepare_payload(payload, metadata, 2, 2.75)


def test_zero_motion_reacquisition_preserves_raw_and_records_support(tmp_path, monkeypatch):
    def render(*args):
        result = static_render(*args)
        result['images'] = {}
        return result
    monkeypatch.setattr(motion.acquisition.reference, '_render_field', render)
    payload = _payload()
    projection = motion.source_projection([payload])
    identity = {'run_id': 'source', 'manifest_sha256': 'a'*64}
    motion.acquisition._camera(tmp_path / 'static', payload,
        {'id': 'condition-000', 'luminance_cd_m2':1000, 'exposure_ms':4}, identity, lambda _:None, lambda:None)
    candidate = motion._camera(tmp_path / 'moving', payload,
        {'id':'condition-000','luminance_cd_m2':1000,'object_speed_m_s':0,
         'common_valid_rect_row_col':[8,248,23,233]}, projection,20,4,1,2.75,identity,lambda _:None,lambda:None)
    static = tmp_path/'static/condition-000/candidates/candidate-01/fields'
    moving = tmp_path/'moving'/candidate['prefix']/'fields'
    for field in motion.acquisition.FIELDS:
        for chart in motion.acquisition.CHARTS:
            for repeat in range(8):
                filename = f'{field}/{chart}/raw-{repeat:02d}.npy'
                assert sha256(static/filename) == sha256(moving/filename)
        capture = json.loads((moving/field/'capture.json').read_text())
        for record in capture['captures']['resolution']['raw']:
            assert record['supported_roi_row_col'] == [8,248,23,233]
            assert record['sfr_support_roi_row_col'] == [100,156,100,156]
            assert all(v == 0 for v in record['sfr_support_saturation_fractions'].values())
        mask = np.load(moving/field/'motion-valid-mask.npy')
        assert mask.sum() == 240*210
    assert candidate['result']['configuration']['isp'] == payload['candidate']['result']['configuration']['isp']


def test_run_uses_global_support_and_separate_detail_artifacts(tmp_path, monkeypatch):
    source = tmp_path/'source'; source.mkdir()
    (source/'manifest.json').write_text(json.dumps({'schema':'camera_reference_workshop_files_v1','files':{}}))
    payloads = [_payload(1), _payload(2)]
    monkeypatch.setattr(motion.acquisition, '_validate_source', lambda reader,summary: payloads)
    seen = []
    def camera(root,payload,cell,projection,depth,exposure,ei,f,identity,notify,check):
        seen.append(copy.deepcopy(cell))
        cid = f"{payload['candidate']['id']}-e{ei:02d}"
        (root / f"conditions/{cell['id']}/candidates/{cid}").mkdir(parents=True)
        return {'id':cid, 'prefix':f"conditions/{cell['id']}/candidates/{cid}/",'result':{'fields':[]}}
    monkeypatch.setattr(motion, '_camera', camera)
    monkeypatch.setattr(motion, 'comparison', lambda root,candidates,fs,policy,roi,base: {
        'rows':[{'fields':[]} for _ in candidates], 'baseline_candidate_id':base})
    monkeypatch.setattr(motion, 'compact_candidate', lambda candidate, digest: {
        **candidate,'detail_sha256':digest})
    request = {'source_run_id':'source','luminance_cd_m2':[1000],'object_speed_m_s':[0,4],
        'object_distance_m':20,'exposure_ms':[1,4,16],'selection_policy':{}}
    summary = {'id':'source','configuration':{'f_numbers':[2.75,4.12]}}
    checks=[]
    result = motion.run(None,tmp_path/'out',request,source,summary,lambda _:None,check=lambda:checks.append(1))
    assert len(checks)==3
    assert len(seen)==12
    assert all(c['common_valid_rect_row_col']==[8,248,23,233] for c in seen)
    assert result['baseline_candidate_id']=='candidate-01-e01'
    assert len(list((tmp_path/'out').glob('conditions/*/candidates/*/result.json')))==12
    assert result['aperture_conditions']['common_support_scope'].startswith('Same rectangle')


def test_compact_index_preserves_binding_and_omits_large_diagnostics():
    source={'paraxial':{'effective_focal_length_mm':24.73},'surfaces':[{'large':True}],
            'provenance':{'large':True},'source_id':'patent'}
    optics={'wavefront_diagnostic':{'big':[0]*100},'mtf_comparison':{'effective':{
        'cycles_per_native_pixel':[0,.5], 'radial':[1,0], 'tangential':[1,0]}},'images':{}}
    candidate={'id':'c','label':'F/4','prefix':'conditions/c/candidates/c/',
        'source_identities':{'lens':{'source':source}},'result':{
        'configuration':{},'pixel':{'cell':{'pixel_pitch_um':2.9,'physical_model':{'huge':True}}},
        'fields':[{'id':'center','optics':optics,'capture_files':{'resolution':{
            'raw':[{'file':'raw-00.npy','sha256':'a'*64}]*8,'rate_sha256':'b'*64}},
            'tests':{'resolution':{'status':'ok','sites':{'center':{'roi':[1,4,1,4],
                'processed_system':{'esf':[1]*100,'lsf':[1]*100,'sfr':[1,.5]}}}}}}]}}
    result=motion.compact_candidate(candidate,'c'*64)
    assert result['detail_file']=='conditions/c/candidates/c/result.json'
    assert result['detail_sha256']=='c'*64
    assert result['source_identities']['lens']['source']['source_id']=='patent'
    assert 'surfaces' not in result['source_identities']['lens']['source']
    field=result['result']['fields'][0]
    assert len(field['capture_files']['resolution']['raw'])==1
    assert 'wavefront_diagnostic' not in field['optics']
    assert 'esf' not in field['tests']['resolution']['sites']['center']['processed_system']
    assert 'esf' in candidate['result']['fields'][0]['tests']['resolution']['sites']['center']['processed_system']

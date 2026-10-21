"""Contract regressions; these fixtures are not solver validation evidence."""
import copy
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform import sensor_structure as model
from camera_platform.sensor_structure_job import all_tasks, qualify, sha, validate_sample
from camera_platform.registry import IntegrityError


def test_manifest_preserves_original_depth_and_source_hash():
    root=Path(__file__).resolve().parents[2]
    path=root/'camerae2e_db/fdtd_tcad/image_sensor_db/sensor_catalog.json'
    if not path.exists(): pytest.skip('Local evidence catalog unavailable')
    before=sha(path)
    bundle=model.source_bundle(root)
    assert bundle['geometry']['dti_depth_um']==6.6
    assert bundle['geometry']['dti_width_um']==.23
    assert bundle['corrections'][0]['original']==.23
    assert sha(path)==before==bundle['catalog_sha256']
    assert bundle['measured_accuracy'] is False
    assert {r['status'] for r in bundle['evidence']}=={'calculation_input','retained_unmodeled'}


@pytest.mark.parametrize('value', [dict(resolutions=[60]),dict(resolutions=[60,float('nan')]),
    dict(guard_cells=1.2),dict(neighborhood=4),dict(wavelength_nm=[700]),dict(unknown=True)])
def test_rejects_invalid_or_unbounded_inputs(value):
    with pytest.raises(ValueError): model.specification(value)


def test_task_identity_includes_controls_and_structure():
    s=model.specification({'variants':['reviewed'],'angles_deg':[0]})
    a=all_tasks(s,{'revision':'a'})
    assert len(a)==8
    assert len({t['id'] for t in a})==8
    b=all_tasks(s,{'revision':'b'})
    assert not {t['id'] for t in a}&{t['id'] for t in b}


def samples(spec):
    out=[]
    for t in all_tasks(spec,{'revision':'test'}):
        out.append(dict(variant=t['variant'],wavelength_nm=t['wavelength_nm'],angle_deg=t['angle_deg'],
            polarization=t['polarization'],resolution=t['resolution'],control=t.get('control','mesh'),
            pixel_absorption=[0,0,.1,.5,.1,0,0],energy_residual=0,passive=True,support_tail_fraction=0))
    return out


def test_2d_cannot_become_camera_response_even_if_all_numeric_checks_pass():
    s=model.specification({'variants':['reviewed'],'angles_deg':[0]})
    _,q=qualify(samples(s),s)
    assert q['status']=='qualified'
    assert q['camera_response_eligible'] is False


def test_missing_resolution_and_control_stay_unqualified():
    s=model.specification({'variants':['reviewed'],'angles_deg':[0]})
    assert model.assess([],s)['status']=='unqualified'
    assert model.assess(samples(s)[:1],s)['status']=='unqualified'
    _,q=qualify([v for v in samples(s) if v['control']=='mesh'],s)
    assert q['status']=='unqualified'
    assert 'time_refinement' in q['checks'][0]['reasons']


def test_nonpassive_and_failed_controls_not_laundered():
    s=model.specification({'variants':['reviewed'],'angles_deg':[0]})
    a=samples(s);a[0]['passive']=False
    assert qualify(a,s)[1]['status']=='unqualified'
    a=samples(s)
    next(v for v in a if v['control']=='pml')['energy_residual']=.02
    assert qualify(a,s)[1]['status']=='unqualified'


def test_sample_import_binds_arrays_and_retains_negative_dissipation(tmp_path):
    t=all_tasks(model.specification({'variants':['reviewed'],'angles_deg':[0]}),{'revision':'r'})[0]
    p=tmp_path/'sample.json';arr=p.with_suffix('.npz')
    np.savez(arr,absorption_density=np.ones((3,3)),role=np.ones((3,3)))
    v=dict(task_id=t['id'],structure_revision='r',array_sha256=sha(arr),
        **{k:t[k] for k in ('dimension','variant','wavelength_nm','angle_deg','polarization','resolution')},
        R=.1,T=.1,side_outflow=0,absorption=.8,active_si_absorption=.8,
        energy_residual=0,incident_power=1,layer_absorption={'active_si':.8},
        pixel_absorption=[0,0,0,.8,0,0,0],support_tail_fraction=0,
        negative_dissipation_integral=-.002,passive=True)
    p.write_text(json.dumps(v))
    assert validate_sample(t,p)['passive'] is False
    arr.write_bytes(b'changed')
    with pytest.raises(IntegrityError,match='checksum'):validate_sample(t,p)


def test_exact_node_response_uses_cell_area_once_and_refuses_2d():
    from camera_platform.sensor_structure_response import photoelectron_rate
    s=model.specification({'dimension':3,'variants':['reviewed'],'angles_deg':[0]})
    a=samples(s)
    for v in a:
        v.update(pixel_absorption=[.1,.2,.3,.1],structure_revision='test',task_id='fixture')
    structure={'revision':'test','geometry':{'pitch_um':2.}}
    args=dict(wavelength_nm=[550],spectral_bin_width_nm=[10],angle_deg=0,variant='reviewed',collection_efficiency=.5)
    result=photoelectron_rate(a,s,structure,np.ones((2,2,1))*1e12,**args)
    assert np.allclose(result['mean_electron_rate'],[[8,16],[24,8]])
    with pytest.raises(ValueError,match='Exact'):
        photoelectron_rate(a,s,structure,np.ones((2,2,1)),**{**args,'angle_deg':10})
    with pytest.raises(ValueError,match='3D'):
        photoelectron_rate(a,{**s,'dimension':2},structure,np.ones((2,2,1)),**args)


def test_api_common_lifecycle_and_cancel(tmp_path):
    from fastapi.testclient import TestClient
    from camera_platform.api import create_app
    root=Path(__file__).resolve().parents[2]
    if not (root/'camerae2e_db/fdtd_tcad/image_sensor_db/sensor_catalog.json').exists():
        pytest.skip('Local evidence catalog unavailable')
    app=create_app(tmp_path/'state',root,start_worker=False,seed_demo=False)
    with TestClient(app) as c:
        token=c.get('/api/v1/session').json()['token']
        headers={'X-Platform-Token':token}
        template=c.get('/api/v1/sensor-structure-studies/template')
        assert template.status_code==200
        submitted=c.post('/api/v1/sensor-structure-studies',json=template.json()['specification'],headers=headers)
        assert submitted.status_code==202,submitted.text
        identity=submitted.json()['id']
        common=c.get('/api/v1/jobs/'+identity).json()
        assert common['detail_path']=='sensor-structure-studies/'+identity
        assert common['status']=='queued'
        assert c.get('/sensor-structure.html').status_code==200
        assert c.get('/sensor-structure.js').status_code==200
        cancelled=c.post('/api/v1/sensor-structure-studies/'+identity+'/cancel',json={},headers=headers)
        assert cancelled.status_code==200
        assert c.get('/api/v1/jobs/'+identity).json()['status']=='cancelled'


def test_nonfinite_and_wrong_coordinates_cannot_qualify():
    s=model.specification({'variants':['reviewed'],'angles_deg':[0]})
    a=samples(s);a[0]['pixel_absorption'][0]=float('nan')
    assert qualify(a,s)[1]['status']=='unqualified'
    a=samples(s)
    for v in a:v['angle_deg']=10
    assert qualify(a,s)[1]['status']=='unqualified'


def test_synthetic_contract_only_capture_calls_real_raw_and_isp():
    from camera_platform.sensor_structure_response import capture
    from camera_platform.electrical import default_electrical_model
    s=model.specification({'dimension':3,'variants':['reviewed'],'angles_deg':[0]})
    a=samples(s)
    for v in a:v.update(pixel_absorption=[.1,.2,.2,.1],structure_revision='fixture',task_id='fixture')
    electrical=default_electrical_model(dict(full_well_e=10000,read_noise_e=2,
        dark_current_e_per_s=1,gain=1,bit_depth=12,black_level_dn=64))
    result=capture(a,s,{'revision':'fixture','geometry':{'pitch_um':2.9}},
        np.ones((16,16,1))*1e15,electrical_model=electrical,exposure_s=.004,seed=31,
        wavelength_nm=[550],spectral_bin_width_nm=[10],angle_deg=0,
        variant='reviewed',collection_efficiency=.75)
    assert result['acquisition']['arrays']['raw_dn'].shape==(16,16)
    assert np.issubdtype(result['acquisition']['arrays']['raw_dn'].dtype,np.integer)
    assert result['isp']['arrays']['rgb'].shape==(16,16,3)

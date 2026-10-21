import copy
import sqlite3
import pytest
from camera_platform import sensor_structure_trend as t
from camera_platform.sensor_structure_trend_job import extra_tasks
from camera_platform.sensor_structure_execution import StatusProbe
from camera_platform.sensor_structure import specification

SPEC=specification({'variants':['reviewed','ocl_shift'],'angles_deg':[0]})

def fixture():
    rows=[]
    for pol in ('s','p'):
      for res,control in ((60,'mesh'),(80,'mesh'),(80,'time'),(80,'pml'),(80,'phase')):
       for variant in SPEC['variants']:
        rows.append(dict(variant=variant,wavelength_nm=550,angle_deg=0,polarization=pol,resolution=res,
            control=control,pixel_absorption=[0,.01,.6 if variant=='reviewed' else .66,.01,0],
            passive=True,energy_residual=0,support_tail_fraction=0))
    return rows

def row(result,metric='central_absorption',pol='s'):
    return next(r for r in result['rows'] if r['metric']==metric and r['polarization']==pol)

def test_stable_magnitude_and_no_camera_promotion():
    result=t.assess(fixture(),SPEC)
    assert row(result)['status']=='magnitude_stable'
    assert row(result)['delta']==pytest.approx(.1)
    assert not result['camera_response_eligible']

def test_common_absolute_drift_does_not_hide_stable_paired_effect():
    samples=fixture()
    for s in samples:
        if s['resolution']==80:s['pixel_absorption']=[x*1.09 for x in s['pixel_absorption']]
    result=t.assess(samples,SPEC)
    assert row(result)['status']=='magnitude_stable'
    assert row(result)['sensitivity']<1e-12

def test_missing_checks_are_unresolved_not_converged():
    result=t.assess([s for s in fixture() if s['resolution']==60],SPEC)
    assert row(result)['status']=='unresolved'
    assert [80,'phase'] in row(result)['missing_checks']

def test_sign_reversal_requests_third_mesh():
    samples=fixture()
    for s in samples:
        if s['variant']=='ocl_shift' and s['resolution']==60:s['pixel_absorption'][2]=.54
    result=t.assess(samples,SPEC)
    assert row(result)['sign_reversal']
    assert row(result)['status']=='unresolved'
    assert any(r['resolution']==100 for r in result['additional_requests'])

def test_small_difference_is_not_amplified_into_a_winner():
    samples=fixture()
    for s in samples:
        if s['variant']=='ocl_shift':s['pixel_absorption'][2]=.600001
    assert row(t.assess(samples,SPEC))['status']=='indistinguishable'

def test_weak_baseline_reports_absolute_units():
    samples=fixture()
    for s in samples:s['pixel_absorption']=[x*.001 for x in s['pixel_absorption']]
    r=row(t.assess(samples,SPEC))
    assert r['weak_baseline'] and r['delta_unit']=='fraction'
    assert r['delta']==pytest.approx(.00006)

def test_weak_neighbor_instability_not_normalized_by_bright_center():
    samples=fixture()
    for s in samples:
        if s['variant']=='ocl_shift':
            s['pixel_absorption'][2]=.6
            s['pixel_absorption'][1]=.0101 if s['resolution']==60 else .02
    r=row(t.assess(samples,SPEC),'neighbor_fraction')
    assert r['status']=='unresolved'
    assert r['sensitivity']>0.01

def test_polarization_opposition_visible_in_unpolarized_result():
    samples=fixture()
    for s in samples:
        if s['variant']=='ocl_shift' and s['polarization']=='p':s['pixel_absorption'][2]=.57
    result=t.assess(samples,SPEC)
    assert row(result,pol='unpolarized')['polarization_opposition']

def test_pml_and_phase_change_effect_not_just_absolute_value():
    for control in ('time','pml','phase'):
        samples=fixture()
        for s in samples:
            if s['variant']=='ocl_shift' and s['control']==control:s['pixel_absorption'][2]=.63
        r=row(t.assess(samples,SPEC))
        assert r['status']=='unresolved'
        assert r['sensitivity']==pytest.approx(.05)

def test_tail_sensitive_neighbor_effect_requests_larger_domain():
    samples=fixture()
    for s in samples:s['support_tail_fraction']=.01
    r=t.assess(samples,SPEC)
    assert any(x['control']=='support' for x in r['additional_requests'])

def test_extra_tasks_pair_baseline_and_keep_identity_independent_of_policy():
    request=dict(variants=['reviewed','ocl_shift'],wavelength_nm=550,angle_deg=0,
        polarization='s',resolution=80,control='phase')
    tasks=extra_tasks([request],SPEC,{'revision':'fixture'})
    assert len(tasks)==2 and all(x['grid_phase_x_cells']==.5 for x in tasks)
    assert all(x['neighborhood']==5 for x in tasks)
    support=extra_tasks([{**request,'control':'support'}],SPEC,{'revision':'fixture'})
    assert all(x['neighborhood']==7 for x in support)

def test_lock_probe_preserves_live_solver_until_bounded_timeout(monkeypatch):
    import camera_platform.sensor_structure_execution as e
    now=[0.];probe=StatusProbe(None,'x',clock=lambda:now[0])
    def locked(*args):raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(e,'fast_status',locked)
    now[0]=30;probe.check()
    now[0]=61
    with pytest.raises(RuntimeError,match='60 seconds'):probe.check()
    monkeypatch.setattr(e,'fast_status',lambda *_:'cancelled')
    now[0]=64
    with pytest.raises(ValueError,match='Cancelled'):probe.check()

def test_limits_cannot_be_relaxed():
    for v in ({'energy_limit':.05},{'budget_seconds':30000},{'magnitude_fraction':.5}):
        with pytest.raises(ValueError):t.policy(v)


def test_invalid_condition_preserves_other_polarization():
    samples=fixture()
    samples[0]['pixel_absorption']=[float('nan')]*5
    result=t.assess(samples,SPEC)
    assert result['invalid_samples']
    assert row(result,pol='p')['status']=='magnitude_stable'
    assert result['status']=='unresolved'


def test_third_mesh_still_drifting_is_unresolved():
    samples=fixture()
    extra=[]
    for sample in samples:
        if sample['resolution']==80 and sample['control']=='mesh':
            sample=copy.deepcopy(sample);sample['resolution']=100
            sample['pixel_absorption']=[v*1.3 for v in sample['pixel_absorption']]
            extra.append(sample)
    assert row(t.assess(samples+extra,SPEC))['status']=='unresolved'


def test_budget_exhaustion_publishes_existing_samples_without_solver(tmp_path,monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from camera_platform import sensor_structure_trend_job as controller
    from camera_platform import design_workshop_storage as storage
    from camera_platform import sensor_structure_report as report
    samples=fixture()
    for i,s in enumerate(samples):s.update(task_id=str(i),array_sha256='array',elapsed_s=1500)
    service=SimpleNamespace(registry=SimpleNamespace(directory=tmp_path),workspace=tmp_path)
    job=dict(id='budget-test',operation='trend',specification=SPEC,assessment_policy=t.defaults(),
        structure={},worker_sha256='hash',phase_worker_sha256='hash',source_job='source',
        sample_directory=str(tmp_path/'samples'),source_samples=[dict(task={'id':s['task_id']},path=str(tmp_path/(s['task_id']+'.json'))) for s in samples])
    monkeypatch.setattr(controller,'read_snapshot',lambda _:samples)
    monkeypatch.setattr(controller.jobs,'sha',lambda _:'hash')
    monkeypatch.setattr(controller.jobs,'all_tasks',lambda *_:[{'id':'missing'}])
    monkeypatch.setattr(controller.jobs,'qualify',lambda *_:([],{'checks':[]}))
    monkeypatch.setattr(controller,'extra_tasks',lambda *_:[])
    monkeypatch.setattr(controller.jobs,'run_one',lambda *a,**kw:pytest.fail('Budget must prohibit a solver'))
    monkeypatch.setattr(controller.jobs,'publish',lambda service,summary,paths:summary)
    monkeypatch.setattr(report,'render_maps',lambda *a,**kw:[])
    @contextmanager
    def reserve(*args,**kwargs):yield tmp_path,lambda:None
    monkeypatch.setattr(storage,'reserve',reserve)
    result=controller.execute(service,job)
    assert result['stop_reason']=='budget_exhausted'
    assert result['completed_samples']==len(samples)
    assert result['status']=='unresolved'

"""Bind a real-die optical preparation and a frozen spectral campaign to RAW/ISP.

Meep runs separately with its own persistent budget and sample-level recovery.
This job waits for that declared campaign, validates every numerical artifact,
then projects only accepted nodes. No 2D result is promoted to qualified 3D QE.
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import numpy as np

from . import design_workshop_storage as storage
from . import sensor_structure_camera as camera
from . import sensor_structure_spectral as spectral
from . import sensor_structure_spectral_camera as projection
from .registry import ConflictError, IntegrityError, canonical, digest
from .sensor_structure_job import sha
from .sensor_structure_trend_job import atomic

KIND = 'sensor_structure_spectral_camera'
ROUTE = '/api/v1/sensor-structure-spectral-studies'
SCHEMA = 'camera_ocl_located_spectral_projection_v1'
WORKER_TIMEOUT_S = 32400
CAMPAIGN = '20261018-imx662-real-cra'


def template():
    return dict(schema=SCHEMA, name='IMX662 실제 센서 CRA · 파장별 OCL → RAW·ISP',
                campaign_id=CAMPAIGN, fields=['center', 'right-edge'],
                charts=['uniform', 'color', 'texture', 'resolution'], light_scales=[.1, 1.],
                exposure_ms=4., gain=1., frames=8, seed=2718,
                interpolation='piecewise_linear_450_550_650_no_extrapolation')


def specification(value):
    if value != template():
        raise ValueError('Use the pinned real-IMX662 spectral camera specification')
    return copy.deepcopy(value)


def prepared(service):
    return service.workspace/'outputs/ocl-spectral-camera-20261018'


def campaign_root(service):
    return storage.root(service)/'ocl-spectral-campaigns'/CAMPAIGN


def fingerprint(service):
    from .sensor_structure_camera_job import fingerprint as camera_fingerprint
    value = camera_fingerprint()
    for name in ('sensor_structure_spectral_job.py', 'sensor_structure_spectral_camera.py',
                 'sensor_structure_spectral.py', 'sensor_structure_spectral_optics.py',
                 'sensor_structure_spectral_provenance.py'):
        value[name] = sha(Path(__file__).parent/name)
    value['periodic_worker'] = sha(spectral.worker_path(service.workspace))
    return value


def submit(service, payload):
    from .service import environment_fingerprint
    if environment_fingerprint() != service.loaded_environment:
        raise ConflictError('Source changed; restart API before submitting')
    spec = specification(payload)
    task_path = prepared(service)/'tasks.json'
    doc = dict(name=spec['name'], job_kind=KIND, specification=spec,
               source_code=fingerprint(service), tasks_sha256=sha(task_path),
               prepared_manifest_sha256=sha(prepared(service)/'prepared-manifest.json'),
               default_worker_timeout_seconds=WORKER_TIMEOUT_S)
    return service.registry.submit(doc, digest(doc))


def evidence_rows(samples):
    rows = spectral.ratios(samples)
    result = []
    keys = {(r['field'], r['wavelength_nm'], r['cfa_channel']) for r in rows}
    for field, wave, channel in sorted(keys):
        group = sorted((r for r in rows if (r['field'], r['wavelength_nm'], r['cfa_channel']) == (field, wave, channel)
                        and r['control'] == 'mesh'), key=lambda r: r['resolution'])
        hi = group[-1]
        valid = len(group) >= 2 and all(r['numerical_valid'] for r in group)
        difference = hi.get('absolute_delta')
        sensitivity = None
        status = 'unresolved'
        if valid:
            sensitivity = max(abs(r['absolute_delta']-difference) for r in group)
            signs = {int(np.sign(r['absolute_delta'])) for r in group}
            if len(signs) > 1:
                status = 'unresolved'
            elif hi['weak_response'] or abs(difference) < .01*hi['baseline_absorption'] or abs(difference) <= 2*sensitivity:
                status = 'indistinguishable'
            elif len(signs) == 1:
                status = 'two_grid_stable' if sensitivity <= .2*abs(difference) else 'direction_stable'
        same = [s for s in samples if s['field'] == field and s['wavelength_nm'] == wave and s['cfa_channel'] == channel]
        result.append(dict(field_id=field, wavelength_nm=wave, cfa_channel=channel,
            angle_deg=hi['angle_deg'], baseline_absorption=hi['baseline_absorption'],
            shifted_absorption=hi['changed_absorption'], response_gain=hi['gain'],
            absolute_absorption_delta=difference, observed_grid_sensitivity=sensitivity,
            weak_response=hi['weak_response'], numerical_validity=bool(valid), trend_status=status,
            mesh_gain_change=(abs(group[0]['gain']-hi['gain']) if valid and all(r['gain'] is not None for r in group) else None),
            energy_residual_max=max(abs(s['energy_residual']) for s in same),
            polarized=hi.get('polarized'), completed_checks=['energy', 'passivity', 'grid60', 'grid80'] if valid else [],
            polarization_opposed=(len({int(np.sign(s['absolute_delta'])) for s in hi.get('polarized', [])}) > 1),
            pending_checks=['time_refinement', 'pml_refinement', 'half_cell_phase'],
            scope='2D uniform-color periodic plane wave; two-grid trend screen, not full convergence or 3D QE'))
    return result


def assumptions():
    return [dict(id=k, label=label, classification=status, description=text) for k, label, status, text in (
        ('dimensions', '실제 센서 범위', 'official_specification', 'Sony IMX662-AAQR/AAQR1 active 1937×1097, pitch2.9µm. 중심·오른쪽 256×256 RAW 탐침은 모두 active 영역 안에 있습니다.'),
        ('placement', '광학 배치', 'optical_mounting_assumption', '렌즈축을 active 중심에 배치하고 RGGB 원점을 가정. 오른쪽 탐침 x=2.436mm에서 파장별 chief ray를 재추적했습니다. 실제 모듈 조립 오차는 미반영입니다.'),
        ('source', '새 OCL 해석', 'numerical_evidence', 'R/G/B CFA 재료 각각에 450/550/650nm, s/p 평면파를 입사. 기준/OCL +0.29µm를 계산한 국소 균일 조명 흡수 비율입니다. 이전 유한 빔 중심 픽셀 배율을 재사용하지 않습니다.'),
        ('materials', '구조·재료', 'model_proxy', 'TechInsights 기반 IMX662 기하와 기존 n,k proxy 사용. Green CFA 두께는 구조 자료, R/B 0.475µm·OCL 타원 단면·DTI liner 모델은 가정입니다. 균일색 2D 주기 배열이며 실제 3D Bayer가 아닙니다.'),
        ('spectrum', '분광 연결', 'explicit_hypothesis', '각 CFA의 세 파장 흡수 비율을 450–650nm 안에서 선형 보간해 21개 광전자율 대역에 적용한 후 합산합니다. 대역 밖 외삽은 없으며 세 파장 사이의 구조는 미확인입니다.'),
        ('pupil', '동공과 공간 범위', 'explicit_hypothesis', '기존 동공 평균 planar QE·픽셀 적분·새 위치의 PSF에 chief CRA의 2D 상대효과를 적용합니다. OCL 효과 자체의 동공 전체 적분, 3D 공간 전달 kernel, 모듈 QE는 아닙니다.'),
        ('capture', 'RAW·ISP', 'fixed_policy', 'ASI662MC readout proxy·공통 기존 전자→RGB 행렬 고정. 4ms/gain1, 8회 독립 취득, Bilinear/WB1/LSC OFF/NR OFF, 동일 표시 white. 광전자율 변경 후 잡음·포화·ADC를 다시 계산합니다.'),
        ('numerics', '수치 확인 범위', 'not_validated', '에너지 잔차1%·무원성 유지, 60/80 cells/µm 비교. 시간·PML·격자 위치 추가 확인을 끝낸 수렴 판정과 구분합니다. 작거나 격자 민감도에 묻힌 차이로 구조 우열을 주장하지 않습니다.'),
    )]


def write_case(folder, baseline_rate, changed_rate, cell, calibration, geometry, *, seed, chart, light):
    from .sensor_structure_camera_job import _png
    from .reference_workshop_engine import write_json
    folder.mkdir(parents=True, exist_ok=True)
    base = camera.acquire(baseline_rate, cell['electrical_model'], seed, geometry=geometry)
    changed = camera.acquire(changed_rate, cell['electrical_model'], seed, geometry=geometry)
    pb = camera.process(base, calibration['color']['matrix'], None)
    pc = camera.process(changed, calibration['color']['matrix'], None)
    common_raw = np.all(base['valid_frames'] & changed['valid_frames'], axis=0)
    common_rgb = np.all(pb['valid'] & pc['valid'], axis=0)
    for mask in (common_raw, common_rgb):
        mask[:8] = False; mask[-8:] = False; mask[:, :8] = False; mask[:, -8:] = False
    mb = camera.measure(base, pb, common_raw, common_rgb)
    mc = camera.measure(changed, pc, common_raw, common_rgb)
    metrics = dict(baseline=mb, projected=mc,
        comparison=camera.compare(base, changed, pb, pc, mb, mc, common_rgb, chart, light))
    arrays = dict(baseline_mean_rate=baseline_rate, projected_mean_rate=changed_rate,
                  common_raw_valid=common_raw, common_rgb_valid=common_rgb)
    for name, acquired, processed in (('baseline', base, pb), ('projected', changed, pc)):
        for key in ('raw_frames', 'electron_frames', 'well_frames', 'adc_frames', 'valid_frames',
                    'variance_e2', 'expected_photo_e', 'photo_response_map'):
            a = acquired[key]; arrays[name+'_'+key] = a.astype(np.float32) if a.dtype.kind == 'f' else a
        arrays[name+'_linear_frames'] = processed['linear'].astype(np.float32)
        arrays[name+'_expected_linear'] = processed['expected_linear'].astype(np.float32)
        arrays[name+'_calibration_dark_e'] = acquired['calibration']['dark_e']
        np.save(folder/(name+'-raw.npy'), acquired['raw_frames'])
        write_json(folder/(name+'-capture.json'), dict(summary=acquired['capture_summary'],
            calibration={k:v for k,v in acquired['calibration'].items() if k != 'dark_e'},
            geometry=geometry, dark_array=name+'_calibration_dark_e'))
        _png(folder/(name+'-raw.png'), acquired['electron_frames'][0]/cell['readout']['full_well_e'])
        _png(folder/(name+'-isp.png'), processed['display'])
    _png(folder/'difference.png', .5+5*(pc['expected_linear']-pb['expected_linear']))
    np.savez_compressed(folder/'arrays.npz', **arrays)
    return metrics


def execute(service, job):
    from .sensor_structure_execution import StatusProbe
    from .sensor_structure_camera_job import _camera, template as camera_template
    probe = StatusProbe(service.registry, job['id'])
    spec = specification(job['specification'])
    if job['source_code'] != fingerprint(service):
        raise IntegrityError('Spectral camera implementation changed since submission')
    p = prepared(service); campaign = campaign_root(service)
    if sha(p/'tasks.json') != job['tasks_sha256']:
        raise IntegrityError('Frozen numerical tasks changed')
    tasks = json.loads((p/'tasks.json').read_text())
    progress_path = p/(job['id']+'-progress.json')
    started = time.monotonic()
    while True:
        probe.check()
        if time.monotonic()-started > 28800:
            raise ValueError('Declared spectral preparation exceeded eight hours; originals retained')
        status_file = p/'campaign-progress.json'
        status = json.loads(status_file.read_text()) if status_file.exists() else dict(completed=0,total=len(tasks))
        if status.get('status') == 'completed' and (p/'prepared-manifest.json').exists():
            break
        atomic(progress_path, {**status, 'stage':'파장별 OCL 해석 · 실제 센서 광학 입력 준비'})
        if status.get('status') in ('failed', 'budget_exhausted', 'cancelled', 'completed_with_failures', 'blocked_other_meep', 'resource_limit'):
            raise ValueError('Numerical campaign stopped: '+str(status))
        time.sleep(3)
    manifest = json.loads((p/'prepared-manifest.json').read_text())
    if sha(p/'prepared-manifest.json') != job['prepared_manifest_sha256']:
        raise IntegrityError('Prepared optical manifest changed since submission')
    for relative, checksum in manifest['files'].items():
        path = (p/relative).resolve()
        if not path.is_relative_to(p.resolve()) or sha(path) != checksum:
            raise IntegrityError('Located optical preparation changed: '+relative)
    samples = [spectral.validate_sample(t, campaign/'samples'/(t['id']+'.json'),
                                       worker_sha256=job['source_code']['periodic_worker']) for t in tasks]
    evidence = evidence_rows(samples)
    if len(evidence) != 18:
        raise IntegrityError('Both fields require all nine CFA/wavelength nodes')
    # Validate every field before emitting any camera image. No unity fallback.
    gains = {f:projection.spectral_gains(evidence, f, np.arange(450., 651., 10.)) for f in spec['fields']}
    _, _, cell, calibration, refs = _camera(service, camera_template())
    from .sensor_structure_spectral_provenance import validate_prepared
    crosslink = validate_prepared(p, tasks, cell, manifest)
    located = json.loads((p/'prepared/located-geometry.json').read_text())
    geometry = copy.deepcopy(located['geometry'])
    geometry['source_profile_sha256'] = geometry.pop('profile_sha256')
    for roi in geometry['roi_layout']:
        if roi['id'] == 'outer-ne': roi['id'] = 'right-edge'
    geometry['profile_sha256'] = digest(geometry)
    with storage.reserve(service, 2_000_000_000, allow_internal_aliases=True) as (root, check):
        directory = root/'sensor-structure-spectral-camera'/job['id']; directory.mkdir(parents=True, exist_ok=True)
        atomic(directory/'numerical-evidence.json', evidence)
        atomic(directory/'geometry.json', geometry)
        atomic(directory/'specification.json', spec)
        atomic(directory/'calibration.json', calibration)
        atomic(directory/'pixel-cell.json', cell)
        atomic(directory/'input-manifest.json', manifest)
        atomic(directory/'input-crosslinks.json', crosslink)
        # Carry the exact pre-noise optical inputs into the same external
        # package as RAW outputs; provenance must not depend on a workspace cache.
        import shutil
        for relative, checksum in manifest['files'].items():
            target = directory/'inputs'/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p/relative, target)
            if sha(target) != checksum:
                raise IntegrityError('Copied located optical input changed')
        atomic(directory/'numerical-source.json', dict(campaign_id=CAMPAIGN,
            samples=[dict(task_id=t['id'],json_sha256=sha(campaign/'samples'/(t['id']+'.json')),
                          array_sha256=s['array_sha256']) for t,s in zip(tasks,samples,strict=True)]))
        cases = []
        for fi, field in enumerate(spec['fields']):
            key = 'outer-ne' if field == 'right-edge' else field
            roi = next(r for r in geometry['roi_layout'] if r['id'] == field)
            native = dict(full_sensor_shape=geometry['active_shape_row_col'],crop_origin=roi['origin_row_col'])
            angle = next(e['angle_deg'] for e in evidence if e['field_id']==field and e['wavelength_nm']==550.)
            for ci, chart in enumerate(spec['charts']):
                bands = np.load(p/'prepared/spectral'/key/chart/'spectral-planes-electron-rate.npy',allow_pickle=False)
                baseline, _ = projection.integrate(bands, np.ones((21,4)))
                changed, _ = projection.integrate(bands, gains[field])
                for light in spec['light_scales']:
                    probe.check(); identity=f'case-{len(cases):03d}'; folder=directory/'cases'/identity
                    seed=spec['seed']+fi*10000+ci*100+(10 if light==1 else 0)
                    metrics=write_case(folder, baseline*light,changed*light,cell,calibration,native,
                                       seed=seed,chart=chart,light=light)
                    prefix=f'cases/{identity}/'
                    case=dict(id=identity,field_id=field,chart=chart,light_scale=light,
                        profile_id='spectral_response',host_cra_deg=angle,applied_angle_deg=angle,
                        assessment_scope='Exploratory 2D spectral projection; consult each wavelength grid sensitivity, not fully converged camera prediction',
                        source_geometry=roi,frame_seeds=list(range(seed,seed+8)),metrics=metrics,
                        response_gain_by_wavelength=gains[field].tolist(),
                        difference_display='0.5 + 5×(changed − baseline), noiseless linear RGB; fixed scale',
                        files={k:prefix+v for k,v in dict(baseline_raw='baseline-raw.png',projected_raw='projected-raw.png',
                            baseline_isp='baseline-isp.png',projected_isp='projected-isp.png',arrays='arrays.npz',
                            difference='difference.png',metrics='metrics.json',baseline_raw_array='baseline-raw.npy',
                            projected_raw_array='projected-raw.npy').items()})
                    atomic(folder/'metrics.json',case); cases.append(case)
                    atomic(progress_path,dict(completed=len(cases),total=16,stage=field+' · '+chart+' · RAW/ISP'))
            check()
        summary=dict(schema=SCHEMA,title=spec['name'],status='2d_spectral_sensitivity_only',
            camera_response_eligible=False,measured_accuracy=False,product_mode=False,
            geometry=geometry,source=dict(host_sensor='IMX662 · 실제 active 영역',camera_files=refs,
                campaign_id=CAMPAIGN,structure_revision=tasks[0]['structure']['revision'],
                camera_run_id=camera_template()['camera_run_id'], camera_candidate_id=camera_template()['camera_candidate_id'],
                optical_revision=manifest['optical_revision'],vendor_source=geometry['dimension_source']),
            evidence=evidence,assumptions=assumptions(),cases=cases,
            conditions=dict(exposure_ms=4.,gain=1.,frames=8,illumination='D65 / 1000 cd/m² × light scale',
                demosaic='bilinear',display_white_level=1.,lsc_enabled=False,light_scales=[.1,1.],
                solver_samples=len(samples),wavelength_nodes_nm=[450,550,650],spectral_bins_nm=list(range(450,651,10)),
                color_calibration='fixed common source matrix; no OCL-specific color refit'),
            elapsed_s=time.monotonic()-started)
        atomic(directory/'result.json',summary)
        files={str(f.relative_to(directory)):sha(f) for f in directory.rglob('*') if f.is_file() and not f.name.startswith('._')}
        atomic(directory/'manifest.json',dict(schema=SCHEMA+'_files',files=files))
        summary['stored_bytes']=sum((directory/f).stat().st_size for f in files)
        artifacts=[service.registry.store_file(directory/'manifest.json','manifest.json','application/json'),
                   service.registry.store_blob(canonical(summary).encode(),'study.json','application/json')]
        return dict(summary=summary,artifacts=artifacts)


def get(service, identity):
    job=service.registry.get_run(identity)
    if job.get('job_kind')!=KIND: raise ValueError('Select a spectral OCL camera study')
    progress=prepared(service)/(identity+'-progress.json')
    if job['status']=='running' and progress.exists(): job['progress']=json.loads(progress.read_text())
    return job


def install_routes(app, service):
    from fastapi.responses import FileResponse
    @app.get(ROUTE+'/template')
    def example(): return dict(specification=template())
    @app.get(ROUTE+'/latest')
    def latest():
        jobs=service.registry.list_runs(job_kinds=[KIND]);return dict(id=jobs[0]['id'] if jobs else None)
    @app.post(ROUTE,status_code=202)
    def create(payload:dict): return submit(service,payload)
    @app.get(ROUTE+'/{identity}')
    def detail(identity:str): return get(service,identity)
    @app.post(ROUTE+'/{identity}/cancel')
    def cancel(identity:str): get(service,identity);return service.registry.cancel(identity)
    @app.get(ROUTE+'/{identity}/files/{name:path}')
    def file(identity:str,name:str):
        job=get(service,identity)
        if job['status']!='succeeded': raise ValueError('Camera results are not complete')
        artifact=next(a for a in job['result']['artifacts'] if a['name']=='manifest.json')
        bound=service.registry.artifact_binding(artifact['sha256'],owner_kind='runs',owner_id=identity)
        files=json.loads(Path(bound['path']).read_text())['files']
        if name not in files: raise ValueError('Unknown result artifact')
        root=(storage.root(service)/'sensor-structure-spectral-camera'/identity).resolve(); path=(root/name).resolve()
        if not path.is_relative_to(root) or sha(path)!=files[name]: raise IntegrityError('Result artifact changed')
        return FileResponse(path,media_type='image/png' if path.suffix=='.png' else 'application/json' if path.suffix=='.json' else 'application/octet-stream',
            filename=path.name,content_disposition_type='inline' if path.suffix in ('.png','.json') else 'attachment')

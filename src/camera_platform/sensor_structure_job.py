"""Durable, resumable structure characterization and hash-bound external import."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import numpy as np
import psutil

from . import sensor_structure as model
from .registry import canonical, digest, IntegrityError, ConflictError

KIND='sensor_structure_study'
ROUTE='/api/v1/sensor-structure-studies'
WORKER_TIMEOUT_S=86400
SOLVER=Path('/Users/seongcheoljeong/FDTD/.meep-env/bin/python')


def sha(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()


def worker_path(service):
    return service.workspace/'simulations/fdtd_tcad/sensor_structure_worker.py'


def all_tasks(spec,bundle):
    result=model.tasks(spec,bundle)
    for t in list(result):
        if t['resolution']!=max(spec['resolutions']): continue
        for control in ('time','pml'):
            extra=copy.deepcopy(t);extra.pop('id');extra['control']=control
            if control=='time': extra['after_source_time']*=2
            else: extra['pml_um']*=1.5
            extra['id']=digest(extra);result.append(extra)
    # Interleave structures so partial results are useful without changing tasks.
    order={name:i for i,name in enumerate(model.VARIANTS)}
    return sorted(result,key=lambda t:(t.get('control','mesh')!='mesh',t['resolution'],t['wavelength_nm'],t['angle_deg'],t['polarization'],order[t['variant']],t.get('control','mesh')))


def submit(service,payload):
    from .service import environment_fingerprint
    if environment_fingerprint()!=service.loaded_environment:
        raise ConflictError('Source changed; restart API before submitting')
    spec=model.specification(payload)
    bundle=model.source_bundle(service.workspace)
    doc=dict(name=spec['name'],job_kind=KIND,specification=spec,structure=bundle,
             worker_sha256=sha(worker_path(service)),environment=service.loaded_environment,
             operation='study',default_worker_timeout_seconds=WORKER_TIMEOUT_S)
    return service.registry.submit(doc,digest(doc))


def validate_sample(task, path):
    v=json.loads(Path(path).read_text()); arr=Path(path).with_suffix('.npz')
    if v['task_id']!=task['id'] or v['structure_revision']!=task['structure']['revision'] or sha(arr)!=v['array_sha256']:
        raise IntegrityError('Result identity / numerical array checksum mismatch')
    for name in ('dimension','variant','wavelength_nm','angle_deg','polarization'):
        if v[name]!=task[name]: raise IntegrityError('Sample coordinate mismatch: '+name)
    if v['resolution']!=task['resolution'] or v.get('control','mesh')!=task.get('control','mesh'):
        raise IntegrityError('Sample resolution / control mismatch')
    numbers=[v[k] for k in ('R','T','side_outflow','absorption','active_si_absorption','energy_residual','incident_power')]
    if not np.isfinite(numbers).all() or not np.isfinite(v['pixel_absorption']).all() or v['incident_power']<=0:
        raise IntegrityError('Nonfinite or invalid physical response')
    expected=task['neighborhood']+2*task['guard_cells'] if task['dimension']==2 else 4
    if len(v['pixel_absorption'])!=expected: raise IntegrityError('Native response axis mismatch')
    residual=1-v['R']-v['T']-v['side_outflow']-sum(v['layer_absorption'].values())
    if not np.isclose(residual,v['energy_residual'],atol=1e-9): raise IntegrityError('Energy residual does not match powers')
    if not np.isclose(sum(v['layer_absorption'].values()),v['absorption'],atol=1e-9):
        raise IntegrityError('Total absorption does not match layer powers')
    if not np.isclose(sum(v['pixel_absorption']),v['active_si_absorption'],rtol=1e-6,atol=1e-9):
        raise IntegrityError('Active absorption does not match native sites')
    guard=task['guard_cells']
    expected_tail=(sum(v['pixel_absorption'][:guard])+sum(v['pixel_absorption'][-guard:]))/max(v['active_si_absorption'],1e-15) if task['dimension']==2 else 0.
    if not np.isclose(v['support_tail_fraction'],expected_tail,atol=1e-9):
        raise IntegrityError('Support tail does not match native response')
    v['passive']=min(v['R'],v['T'],v.get('negative_dissipation_integral',float('-inf')),*v['pixel_absorption'],*v['layer_absorption'].values())>=-1e-5
    with np.load(arr,allow_pickle=False) as a:
        d=a['absorption_density']; role=a['role']
        if d.shape!=role.shape or not np.isfinite(d).all() or np.min(d)<0:
            raise IntegrityError('Invalid absorption map')
    return v


def qualify(samples,spec):
    base=[copy.deepcopy(v) for v in samples if v.get('control','mesh')=='mesh']
    def key(v): return tuple(v[k] for k in ('variant','wavelength_nm','angle_deg','polarization','resolution'))
    for b in base:
        if b['resolution']!=max(spec['resolutions']): continue
        for name in ('time','pml'):
            control=next((v for v in samples if v.get('control')==name and key(v)==key(b)),None)
            delta=None if control is None else float(np.max(np.abs(np.array(control['pixel_absorption'])-b['pixel_absorption']))/max(np.max(b['pixel_absorption']),.01))
            b[name+'_refinement_passed']=control is not None and delta<=(.05 if spec['dimension']==2 else .03) and abs(control['energy_residual'])<=.01 and control['passive']
            b[name+'_relative_change']=delta
    assessment=model.assess(base,spec)
    return base,assessment


def run_one(service,job,task,folder, *, solver_script=None, deadline=None):
    path=folder/(task['id']+'.json'); request=folder/(task['id']+'.task.json')
    if path.exists(): return validate_sample(task,path),True
    request.write_text(canonical(task))
    env={**os.environ,'OMP_NUM_THREADS':'4','OPENBLAS_NUM_THREADS':'1'}
    from .sensor_structure_execution import StatusProbe,BudgetExhausted
    probe=StatusProbe(service.registry,job['id'])
    started=time.monotonic();peak=0
    temporary=path.with_name(path.stem+'.partial.json')
    with path.with_suffix('.log').open('w') as log:
        process=subprocess.Popen([str(SOLVER),str(solver_script or worker_path(service)),str(request),str(temporary)],
            stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=True)
        try:
            while process.poll() is None:
                if deadline is not None and time.monotonic()>=deadline:raise BudgetExhausted('Eight-hour budget reached; precision not reduced')
                probe.check()
                p=psutil.Process(process.pid)
                try: rss=sum(m.memory_info().rss for m in [p,*p.children(recursive=True)] if m.is_running())
                except psutil.NoSuchProcess: rss=0
                peak=max(peak,rss)
                if peak>job['specification']['max_rss_gib']*1024**3: raise ValueError('Memory budget exceeded; resolution not reduced')
                if time.monotonic()-started>job['specification']['max_wall_seconds']: raise ValueError('Solver time budget exceeded')
                time.sleep(.5)
            if process.returncode: raise ValueError('Meep worker failed: '+path.with_suffix('.log').read_text()[-1200:])
        finally:
            if process.poll() is None:
                import signal
                os.killpg(process.pid,signal.SIGTERM)
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: os.killpg(process.pid,signal.SIGKILL);process.wait()
    v=validate_sample(task,temporary);v['observed_peak_rss_bytes']=peak
    temporary.write_text(canonical(v))
    temporary.with_suffix('.npz').replace(path.with_suffix('.npz'))
    temporary.replace(path)
    return v,False


def export_external(service,job,folder,tasks):
    directory=folder/'external';directory.mkdir(exist_ok=True)
    (directory/'worker.py').write_bytes(worker_path(service).read_bytes())
    for t in tasks: (directory/(t['id']+'.task.json')).write_text(canonical(t))
    manifest=dict(schema='camera_sensor_structure_external_v1',study_id=job['id'],
        structure_revision=job['structure']['revision'],worker_sha256=job['worker_sha256'],
        task_ids=[t['id'] for t in tasks],specification=job['specification'])
    (directory/'manifest.json').write_text(canonical(manifest))
    (directory/'run.py').write_text('''import hashlib,json,subprocess,sys
from pathlib import Path
p=Path(__file__).resolve().parent
m=json.loads((p/'manifest.json').read_text())
assert hashlib.sha256((p/'worker.py').read_bytes()).hexdigest()==m['worker_sha256']
for key in m['task_ids']:
    target=p/(key+'.json')
    if target.exists():
        result=json.loads(target.read_text())
        if result.get('task_id')==key and hashlib.sha256(target.with_suffix('.npz').read_bytes()).hexdigest()==result['array_sha256']: continue
        raise RuntimeError('Existing result mismatch; preserve it for inspection')
    subprocess.run([sys.executable,str(p/'worker.py'),str(p/(key+'.task.json')),str(target)],check=True)
''')
    (directory/'README.txt').write_text('Run python run.py with Meep + NumPy + SciPy on a high-memory host. One process at a time. Copy the complete external directory back under the existing T7 CameraE2E project and use Import. Never edit frozen tasks. 3D absorption is not a spatial crosstalk kernel or physical product validation.\n')
    package=folder/'external.zip'
    with zipfile.ZipFile(package,'w',zipfile.ZIP_DEFLATED) as z:
        for p in directory.iterdir(): z.write(p,p.name)
    return package


def publish(service,summary,paths):
    artifacts=[]
    for p in paths:
        media='image/png' if p.suffix=='.png' else 'application/json' if p.suffix=='.json' else 'application/octet-stream'
        artifacts.append(service.registry.store_file(p,p.name,media))
    artifacts.append(service.registry.store_blob(canonical(summary).encode(),'study.json','application/json'))
    return dict(summary=summary,artifacts=artifacts)


def execute(service,job):
    if job.get('operation') in ('trend','trend_assess'):
        from .sensor_structure_trend_job import execute as trend_execute
        return trend_execute(service,job)
    from . import design_workshop_storage as storage
    spec=job['specification']; task_list=all_tasks(spec,job['structure'])
    if sha(worker_path(service))!=job['worker_sha256']: raise IntegrityError('Worker changed since submission')
    estimated_bytes=2_000_000_000
    if job.get('operation')=='import':
        directory=Path(job['import_directory']).resolve()
        if not directory.is_relative_to(storage.root(service).resolve()):
            raise ValueError('Import directory must be inside project storage')
        estimated_bytes+=sum((directory/(t['id']+ext)).stat().st_size for t in task_list for ext in ('.json','.npz'))
    with storage.reserve(service,estimated_bytes) as (root,check):
        folder=root/'sensor-structure-studies'/digest(dict(spec=spec,bundle=job['structure'],worker=job['worker_sha256']))
        folder.mkdir(parents=True,exist_ok=True)
        samples_dir=folder/'samples';samples_dir.mkdir(exist_ok=True)
        (folder/'structure.json').write_text(canonical(job['structure']))
        if spec['dimension']==3 and job.get('operation')!='import':
            package=export_external(service,job,folder,task_list)
            return publish(service,dict(status='awaiting_external_3d',specification=spec,
                structure=job['structure'],task_count=len(task_list),camera_response_eligible=False,
                estimated_3d_voxel_bytes=int((2*2.9*max(spec['resolutions']))**2*12*max(spec['resolutions'])*512)),
                [folder/'structure.json',package])
        samples=[]; reused=0; started=time.monotonic()
        for i,t in enumerate(task_list):
            from .sensor_structure_execution import StatusProbe
            StatusProbe(service.registry,job['id']).check()
            if job.get('operation')=='import':
                directory=Path(job['import_directory']).resolve()
                if not directory.is_relative_to(root.resolve()): raise ValueError('Import directory must be inside project storage')
                manifest=json.loads((directory/'manifest.json').read_text())
                if manifest.get('study_id')!=job['source_job'] or manifest.get('structure_revision')!=job['structure']['revision'] or manifest.get('task_ids')!=[x['id'] for x in task_list]:
                    raise IntegrityError('External manifest does not match source study')
                if sha(directory/'worker.py')!=job['worker_sha256']: raise IntegrityError('External worker changed')
                if json.loads((directory/(t['id']+'.task.json')).read_text())!=t: raise IntegrityError('External task changed')
                v=validate_sample(t,directory/(t['id']+'.json'));reuse=True
            else: v,reuse=run_one(service,job,t,samples_dir)
            samples.append(v);reused+=int(reuse)
            partial,assessment=qualify(samples,spec)
            progress=dict(run_id=job['id'],completed=i+1,total=len(task_list),reused=reused,
                elapsed_s=time.monotonic()-started,last=dict(variant=v['variant'],energy_residual=v['energy_residual']),
                samples=partial,assessment=assessment)
            pending=folder/'progress.tmp';pending.write_text(canonical(progress));pending.replace(folder/'progress.json')
            print(canonical({k:v for k,v in progress.items() if k not in ('samples','assessment')}),flush=True)
            if i%6==0: check()
        base,assessment=qualify(samples,spec)
        summary=dict(schema=model.SCHEMA,status=assessment['status'],assessment=assessment,
            structure=job['structure'],specification=spec,samples=base,
            completed_samples=len(samples),reused_samples=reused,elapsed_s=time.monotonic()-started,
            camera_response_eligible=assessment['camera_response_eligible'],measured_accuracy=False)
        (folder/'samples.json').write_text(canonical(samples))
        paths=[folder/'structure.json',folder/'samples.json']
        # All per-sample arrays are retained on SSD; ZIP binds them for download/import.
        archive=folder/'numerical-results.zip'
        data_dir=Path(job['import_directory']) if job.get('operation')=='import' else samples_dir
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
            for t in task_list:
                for suffix in ('.json','.npz'):
                    p=data_dir/(t['id']+suffix);z.write(p,p.name)
        paths.append(archive)
        from .sensor_structure_report import render_maps
        paths.extend(render_maps(folder,base,data_dir,spec))
        return publish(service,summary,paths)


def get(service,identity):
    job=service.registry.get_run(identity)
    if job.get('job_kind')!=KIND: raise ValueError('Select a sensor structure study')
    if job['status']=='running' and job.get('operation') in ('trend','trend_assess'):
        from .sensor_structure_trend_job import progress_path
        p=progress_path(service,job)
        if p.exists():job['progress']=json.loads(p.read_text())
        return job
    if job['status']=='running':
        from .design_workshop_storage import root
        folder=root(service)/'sensor-structure-studies'/digest(dict(spec=job['specification'],bundle=job['structure'],worker=job['worker_sha256']))
        p=folder/'progress.json'
        if p.exists():
            v=json.loads(p.read_text())
            if v['run_id']==identity:job['progress']=v
    return job


def install_routes(app,service):
    from fastapi.responses import FileResponse
    from .sensor_structure_trend_job import install_routes as install_trend
    install_trend(app,service)
    @app.get(ROUTE+'/template',tags=['sensor_structure'])
    def template(): return dict(specification=model.defaults(),structure=model.source_bundle(service.workspace))
    @app.get(ROUTE,tags=['sensor_structure'])
    def listing(): return service.registry.list_runs(job_kinds=[KIND])
    @app.post(ROUTE,status_code=202,tags=['sensor_structure'])
    def create(payload:dict): return submit(service,payload)
    @app.get(ROUTE+'/{identity}',tags=['sensor_structure'])
    def detail(identity:str): return get(service,identity)
    @app.post(ROUTE+'/{identity}/cancel',tags=['sensor_structure'])
    def cancel(identity:str): get(service,identity);return service.registry.cancel(identity)
    @app.post(ROUTE+'/{identity}/import',status_code=202,tags=['sensor_structure'])
    def importing(identity:str,payload:dict):
        source=get(service,identity)
        if source['specification']['dimension']!=3 or source['status']!='succeeded': raise ValueError('Select a completed 3D export')
        if set(payload)!={'directory'}: raise ValueError('Supply external result directory')
        doc={k:source[k] for k in ('name','job_kind','specification','structure','worker_sha256','environment')}
        doc.update(operation='import',source_job=identity,import_directory=str(Path(payload['directory']).resolve()),default_worker_timeout_seconds=WORKER_TIMEOUT_S)
        return service.registry.submit(doc,digest(doc))
    @app.get(ROUTE+'/{identity}/files/{name}',tags=['sensor_structure'])
    def file(identity:str,name:str):
        job=get(service,identity)
        if job['status']!='succeeded': raise ValueError('Result not complete')
        a=next((a for a in job['result']['artifacts'] if a['name']==name),None)
        if a is None: raise ValueError('Unknown result artifact')
        binding=service.registry.artifact_binding(a['sha256'],owner_kind='runs',owner_id=identity)
        return FileResponse(binding['path'],media_type=a['media_type'],filename=name,
                            content_disposition_type='inline' if a['media_type']=='image/png' else 'attachment')

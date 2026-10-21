"""Versioned assessments reuse solver bytes; adaptive runs have a hard budget."""
from __future__ import annotations
import copy,json,time
from pathlib import Path
from .registry import canonical,digest,IntegrityError,ConflictError
from . import sensor_structure as model
from . import sensor_structure_trend as trend
from . import sensor_structure_job as jobs
from .sensor_structure_execution import BudgetExhausted,StatusProbe


def phase_path(service):return service.workspace/'simulations/fdtd_tcad/sensor_structure_phase_worker.py'
def progress_path(service,job):return service.registry.directory/'sensor-trend'/job['id']/'progress.json'
def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(canonical(value));tmp.replace(path)

def source_folder(service,job):
    from .design_workshop_storage import root
    if job.get('operation') in ('trend','trend_assess'):return Path(job['sample_directory'])
    return root(service)/'sensor-structure-studies'/digest(dict(spec=job['specification'],bundle=job['structure'],worker=job['worker_sha256']))/'samples'

def snapshot(service,source):
    folder=source_folder(service,source);found=list(source.get('source_samples',[]))
    candidates=jobs.all_tasks(source['specification'],source['structure'])
    if source.get('operation') in ('trend','trend_assess'):
        candidates=[json.loads(p.read_text()) for p in folder.glob('*.task.json') if not p.name.startswith('._')]
    for task in candidates:
        path=folder/(task['id']+'.json')
        if not path.exists():continue
        try:v=jobs.validate_sample(task,path)
        except (json.JSONDecodeError,FileNotFoundError):
            if source['status']=='running':continue
            raise
        found.append(dict(task=task,path=str(path),json_sha256=jobs.sha(path),array_sha256=v['array_sha256']))
    return list({r['task']['id']:r for r in found}.values())

def submit(service,identity,payload,operation):
    from .service import environment_fingerprint
    if environment_fingerprint()!=service.loaded_environment:raise ConflictError('Source changed; restart API before submitting')
    if set(payload)-{'policy'}:raise ValueError('Only policy may be changed; solver inputs remain frozen')
    source=jobs.get(service,identity)
    if source['specification']['dimension']!=2 or 'reviewed' not in source['specification']['variants']:
        raise ValueError('Trend study requires a 2D reviewed baseline')
    p=trend.policy(payload.get('policy'))
    doc={k:copy.deepcopy(source[k]) for k in ('name','job_kind','specification','structure','worker_sha256')}
    doc.update(operation=operation,source_job=identity,assessment_policy=p,
        source_samples=snapshot(service,source),reuse_directories=list(dict.fromkeys([str(source_folder(service,source)),*source.get('reuse_directories',[])])),phase_worker_sha256=jobs.sha(phase_path(service)),
        environment=environment_fingerprint(),default_worker_timeout_seconds=32400)
    # Policy is excluded from numerical cache identity. Base task hashes retain
    # their legacy identity; phase controls have a separate frozen worker hash.
    from .design_workshop_storage import root
    doc['sample_directory']=str(root(service)/'sensor-structure-trend-cache'/digest(dict(
        structure=doc['structure']['revision'],worker=doc['worker_sha256'],phase_worker=doc['phase_worker_sha256']))/'samples')
    return service.registry.submit(doc,digest(doc))

def extra_tasks(requests,spec,bundle):
    result={}
    for r in requests:
      for variant in r['variants']:
        task=next(t for t in model.tasks({**spec,'variants':[variant],'wavelength_nm':[r['wavelength_nm']],
            'angles_deg':[r['angle_deg']],'resolutions':[r['resolution']]},bundle) if t['polarization']==r['polarization'])
        task.pop('id');control=r['control']
        if control!='mesh':task['control']=control
        if control=='phase':task['grid_phase_x_cells']=.5
        elif control=='support':task['neighborhood']=7
        elif control=='time':task['after_source_time']*=2
        elif control=='pml':task['pml_um']*=1.5
        task['id']=digest(task);result[task['id']]=task
    return list(result.values())

def read_snapshot(job):
    rows=[]
    for record in job['source_samples']:
        path=Path(record['path'])
        if jobs.sha(path)!=record['json_sha256']:raise IntegrityError('Frozen source sample changed')
        rows.append(jobs.validate_sample(record['task'],path))
    return rows

def execute(service,job):
    spec=job['specification'];p=job['assessment_policy'];folder=progress_path(service,job).parent
    folder.mkdir(parents=True,exist_ok=True)
    samples=read_snapshot(job);paths={r['task']['id']:Path(r['path']) for r in job['source_samples']}
    assessment=trend.assess(samples,spec,p)
    start=time.monotonic();spent=sum(s.get('elapsed_s',0) for s in samples);stop_reason=None
    initial_ids={s['task_id'] for s in samples}
    def progress():
        atomic(progress_path(service,job),dict(run_id=job['id'],completed=len(samples),total=len(jobs.all_tasks(spec,job['structure'])),
            elapsed_s=time.monotonic()-start,reused=len(initial_ids),trend_assessment=assessment,
            samples=[s for s in samples if s.get('control','mesh')=='mesh'],
            assessment=jobs.qualify(samples,spec)[1],budget=dict(limit_s=p['budget_seconds'],used_s=spent+time.monotonic()-start),stop_reason=stop_reason))
    progress()
    if job['operation']=='trend':
        if jobs.sha(jobs.worker_path(service))!=job['worker_sha256'] or jobs.sha(phase_path(service))!=job['phase_worker_sha256']:
            raise IntegrityError('Frozen solver changed; do not reuse new solver results under old identity')
        from . import design_workshop_storage as storage
        with storage.reserve(service,2_000_000_000) as (_,check):
            data=Path(job['sample_directory']);data.mkdir(parents=True,exist_ok=True)
            ledger_path=data.parent/(digest(spec)+'.budget.json')
            ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else dict(discarded_s=0.,pending=False)
            # An unclosed attempt has unknown runtime: conservatively exhaust,
            # rather than reset its allowance after a killed worker.
            if ledger.get('pending'):ledger['discarded_s']=p['budget_seconds']
            ledger['pending']=False;atomic(ledger_path,ledger)
            spent+=ledger['discarded_s']
            # References to source arrays remain immutable; no forced recopy/recompute.
            queue=jobs.all_tasks(spec,job['structure']);attempted=set();probe=StatusProbe(service.registry,job['id'])
            deadline=start+max(0,p['budget_seconds']-spent)
            while True:
                queue.extend(extra_tasks(assessment['additional_requests'],spec,job['structure']))
                known={s['task_id'] for s in samples}
                todo=[t for t in queue if t['id'] not in known|attempted]
                if not todo:break
                if time.monotonic()>=deadline:stop_reason='budget_exhausted';break
                task=todo[0];attempted.add(task['id']);probe.check()
                reused_path=next((Path(d)/(task['id']+'.json') for d in job['reuse_directories'] if (Path(d)/(task['id']+'.json')).exists()),None)
                if reused_path is not None:
                    v=jobs.validate_sample(task,reused_path);samples.append(v);paths[task['id']]=reused_path
                    initial_ids.add(task['id']);spent+=v.get('elapsed_s',0);deadline-=v.get('elapsed_s',0)
                    assessment=trend.assess(samples,spec,p);progress();continue
                script=phase_path(service) if task.get('control')=='phase' else jobs.worker_path(service)
                attempt_start=time.monotonic();ledger['pending']=True;atomic(ledger_path,ledger)
                try:
                    v,reused=jobs.run_one(service,job,task,data,solver_script=script,deadline=deadline)
                except BaseException as error:
                    ledger['discarded_s']+=time.monotonic()-attempt_start
                    ledger['pending']=False;atomic(ledger_path,ledger)
                    if isinstance(error,BudgetExhausted):stop_reason='budget_exhausted';break
                    raise
                else:
                    ledger['pending']=False;atomic(ledger_path,ledger)
                samples.append(v);paths[task['id']]=data/(task['id']+'.json')
                if reused:
                    spent+=v.get('elapsed_s',0);deadline-=v.get('elapsed_s',0)
                assessment=trend.assess(samples,spec,p);progress()
                if len(attempted)%6==0:check()
            progress()
    assessment=trend.assess(samples,spec,p)
    manifest=[dict(task_id=s['task_id'],path=str(paths[s['task_id']]),json_sha256=jobs.sha(paths[s['task_id']]),array_sha256=s['array_sha256']) for s in samples]
    summary=dict(schema='camera_sensor_structure_trend_study_v1',status='unresolved' if stop_reason else assessment['status'],
        specification=spec,structure=job['structure'],samples=[s for s in samples if s.get('control','mesh')=='mesh'],
        assessment=jobs.qualify(samples,spec)[1],trend_assessment=assessment,source_job=job['source_job'],
        completed_samples=len(samples),reused_samples=len(initial_ids),stop_reason=stop_reason,
        camera_response_eligible=False,measured_accuracy=False,budget=dict(limit_s=p['budget_seconds'],used_s=spent+time.monotonic()-start))
    atomic(folder/'sample-manifest.json',manifest);atomic(folder/'samples.json',samples)
    # Rendering reads the exact pinned samples via a temporary path mapping;
    # the scientific data stays at its original SSD location.
    from .sensor_structure_report import render_maps
    maps=render_maps(folder,summary['samples'],None,spec,sample_paths=paths)
    return jobs.publish(service,summary,[folder/'sample-manifest.json',folder/'samples.json',*maps])

def install_routes(app,service):
    @app.post(jobs.ROUTE+'/{identity}/trend-assessments',status_code=202,tags=['sensor_structure'])
    def reassess(identity:str,payload:dict):return submit(service,identity,payload,'trend_assess')
    @app.post(jobs.ROUTE+'/{identity}/trend-studies',status_code=202,tags=['sensor_structure'])
    def resume(identity:str,payload:dict):return submit(service,identity,payload,'trend')

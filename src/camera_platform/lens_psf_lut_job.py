"""Durable Lens DB PSF LUT generation, content-addressed reuse, and pixel adapter."""
from __future__ import annotations

import io
import json
import math
import os
import time
from pathlib import Path

import numpy as np

from . import lens_psf_lut as lut
from .registry import ConflictError, IntegrityError, canonical, digest

JOB_KIND='lens_optical_psf_lut'
WORKER_TIMEOUT_S=21600
ROUTE='/api/v1/lens-psf-luts'
DISK_BACKED_THRESHOLD_BYTES=500_000_000

_PROCESS_SOLVER=None


def _solve_process(workspace,spec,uv):
    """Process-local physical solver; no Registry or storage mutations here."""
    global _PROCESS_SOLVER
    if _PROCESS_SOLVER is None:
        _PROCESS_SOLVER=lut.PatentSolver(workspace,spec)
    before=_PROCESS_SOLVER.trace_count
    a,field=_PROCESS_SOLVER.solve(uv)
    return a,field,_PROCESS_SOLVER.trace_count-before


def binding(workspace,spec):
    from . import db_design_optics as db, reference_workshop_optics as optics
    from . import reference_exit_pupil as ep
    from .virtual_lab_job import sha256
    return dict(lens_source=db.source_metadata(workspace,spec['lens_key']),
                backend=optics._backend(),
                lut_solver_sha256=sha256(Path(lut.__file__)),
                scalar_solver_sha256=sha256(Path(ep.__file__)))


def submit(service,payload):
    from .service import environment_fingerprint
    s=lut.specification(payload)
    env=environment_fingerprint()
    if service.loaded_environment!=env: raise ConflictError('Source changed; restart API before submitting LUT generation')
    document=dict(name=s['name'],job_kind=JOB_KIND,specification=s,
                  pinned_source=binding(service.workspace,s),environment=env,
                  default_worker_timeout_seconds=WORKER_TIMEOUT_S)
    return service.registry.submit(document,digest(document))


def _write(path,value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(canonical(value)); temporary.replace(path)


def execute(service,job):
    from . import design_workshop_storage as storage
    from .virtual_lab_job import sha256
    s=lut.specification(job['specification'])
    pinned=binding(service.workspace,s)
    if pinned!=job['pinned_source']: raise IntegrityError('Lens source changed after submission')
    cache_id=digest(dict(specification={k:v for k,v in s.items() if k!='name'},binding=pinned))
    optical_id=digest(dict(optical_parameters={k:s[k] for k in (
        'lens_key','wavelength_nm','optical_sample_um','optical_size','pupil_samples')},binding=pinned))
    estimate=s['max_field_nodes']*len(s['wavelength_nm'])*s['optical_size']**2*4*3+5_000_000
    started=time.monotonic(); computed=0; reused=0
    with storage.reserve(service,estimate) as (project,check):
        folder=project/'lens-psf-luts'/cache_id
        folder.mkdir(parents=True,exist_ok=True)
        node_folder=project/'lens-psf-lut-nodes'/optical_id
        node_folder.mkdir(parents=True,exist_ok=True)
        solver=None; pool=None; pending={}; parallel_traces=0
        processes=max(1,min(4,int(os.environ.get('CAMERA_LUT_PROCESSES','1'))))
        def progress(message):
            _write(folder/'progress.json',dict(run_id=job['id'],message=message,
                       computed_nodes=computed,reused_nodes=reused,elapsed_s=time.monotonic()-started))
            print(message,flush=True)
            if service.registry.get_run(job['id'])['status']!='running':
                raise ValueError('LUT generation cancelled; completed optical nodes retained')
        def canonical_evaluate(uv):
            nonlocal solver,computed,reused,parallel_traces
            signature=digest(dict(optical_cache=optical_id,uv=uv))
            info=node_folder/(signature+'.json'); array=node_folder/(signature+'.npz')
            if info.exists():
                meta=json.loads(info.read_text())
                if meta['node_identity']!=signature or sha256(array)!=meta['sha256']:
                    raise IntegrityError('Optical LUT node cache checksum mismatch')
                with np.load(array,allow_pickle=False) as z: a=z['density_per_um2']
                reused+=1; return a,meta['field']
            try:
                if uv in pending:
                    progress('Physical field solve · '+str(uv))
                    a,field,count=pending.pop(uv).result()
                    parallel_traces+=count
                else:
                    if solver is None: solver=lut.PatentSolver(service.workspace,s)
                    a,field=solver.solve(uv,progress)
                field['status']='qualified'
            except ValueError as exc:
                # Preserve a physical refusal as a coverage hole, never an
                # analytic substitute or a zero-energy usable PSF.
                a=np.zeros((len(s['wavelength_nm']),s['optical_size'],s['optical_size']),np.float32)
                field=dict(status='refused',reason=str(exc),sensor_pixel_aperture_applied=False)
                progress('Refused optical node retained: '+str(exc)[:180])
            temporary=array.with_suffix('.npz.tmp')
            with temporary.open('wb') as stream: np.savez_compressed(stream,density_per_um2=a)
            temporary.replace(array)
            _write(info,dict(node_identity=signature,sha256=sha256(array),field=field))
            computed+=1; return a,field
        def evaluate(uv):
            # Centered rotational prescriptions have exact sagittal/meridional
            # reflection symmetry. Reflect intensity samples, not interpolated
            # kernels; preserve the physical chromatic displacement and axes.
            canonical_uv=tuple(abs(v) for v in uv)
            a,field=canonical_evaluate(canonical_uv)
            field=dict(field,reflection_from_uv=list(canonical_uv))
            if uv[0]<0: a=a[...,::-1]
            if uv[1]<0: a=a[...,::-1,:]
            if 'chief_sensor_xy_mm' in field:
                field['chief_sensor_xy_mm']=[float(v*(-1 if u<0 else 1))
                    for v,u in zip(field['chief_sensor_xy_mm'],uv)]
                field['azimuth_deg']=math.degrees(math.atan2(uv[1],uv[0])) if any(uv) else 90.
            return a,field
        def prefetch(points):
            nonlocal pool
            if processes==1: return
            from concurrent.futures import ProcessPoolExecutor
            import multiprocessing
            for point in points:
                uv=tuple(abs(v) for v in point)
                signature=digest(dict(optical_cache=optical_id,uv=uv))
                if uv in pending or (node_folder/(signature+'.json')).exists(): continue
                if pool is None:
                    # Native solver/interpolator heaps can accumulate across
                    # hundreds of fields. Retire each process after four nodes;
                    # persisted optical caches remain authoritative across it.
                    pool=ProcessPoolExecutor(max_workers=processes,
                        max_tasks_per_child=4,
                        mp_context=multiprocessing.get_context('spawn'))
                pending[uv]=pool.submit(_solve_process,str(service.workspace),s,uv)
        evaluate.prefetch=prefetch
        decoded=s['max_field_nodes']*len(s['wavelength_nm'])*s['optical_size']**2*4
        work=folder/'density-work.npy'
        store=np.lib.format.open_memmap(work,mode='w+',dtype=np.float32,
            shape=(s['max_field_nodes'],len(s['wavelength_nm']),s['optical_size'],s['optical_size'])) if decoded>DISK_BACKED_THRESHOLD_BYTES else None
        try:
            nodes,density,cells=lut.adaptive_grid(s,evaluate,progress,array_store=store)
        finally:
            if pool is not None: pool.shutdown(wait=True,cancel_futures=True)
        check()
        if binding(service.workspace,s)!=pinned: raise IntegrityError('Lens source changed during generation')
        unresolved=sum(c['status']!='qualified' for c in cells)
        document=dict(schema=lut.SCHEMA,lut_id=cache_id,specification=s,pinned_source=pinned,
            nodes=nodes,cells=cells,status='qualified' if not unresolved else 'partial',
            field_interpolation_qualified=not bool(unresolved),unresolved_cells=unresolved,
            optical_intensity_unit='1/um2',optical_sample_um=s['optical_sample_um'],
            optical_size=s['optical_size'],sensor_pixel_aperture_applied=False,
            array_axes=['field_node','wavelength_nm','row','col'],
            density_normalization='conditional_unit_quadrature_integral_on_retained_support',
            image_axes='positive_x_right_positive_y_up_array_rows_down',
            source_metadata_scope_note='Original source comparison annotations are provenance; this LUT uses its own optical grid and field specification.',
            field_coordinates='object_gnomonic_uv_tan_angles',reference_plane='authored_fixed_sensor_plane',
            psf_anchor='550nm_chief_per_field_preserving_chromatic_displacement',
            absolute_radiometry_included=False,
            limitations=['Conditional scalar optical intensity; absolute throughput/RI is a separate lens response.',
                'Centered rotationally symmetric prescription; radial physical ray bundles reused by symmetry.',
                'Finite-support point samples with declared optical quadrature; sensor aperture applied by adapter only.',
                'Field interpolation checked at cell center and four edge midpoints for every stored wavelength.',
                'No spectral interpolation/extrapolation, autofocus, coating/polarization, 3D occlusion or product validation.',
                'Sensor angular QE and coherent landing-direction coupling remain separate.'],
            statistics=dict(computed_nodes=computed,reused_nodes=reused,
                physical_pupil_bundles_traced=parallel_traces+(solver.trace_count if solver else 0),
                solver_processes=processes,
                field_nodes=len(nodes),wavelength_nodes=len(nodes)*len(s['wavelength_nm']),
                qualified_field_nodes=sum(n.get('status')!='refused' for n in nodes),
                refused_field_nodes=sum(n.get('status')=='refused' for n in nodes),
                elapsed_s=time.monotonic()-started))
        array=folder/('optical-intensity.npy' if store is not None else 'optical-intensity.npz')
        temporary=array.with_suffix(array.suffix+'.tmp')
        with temporary.open('wb') as stream:
            if store is None: np.savez_compressed(stream,density_per_um2=density)
            else: np.save(stream,density,allow_pickle=False)
        temporary.replace(array)
        if store is not None:
            del density
            store._mmap.close()
            work.unlink()
        document['storage_format']='npy_memmap' if store is not None else 'npz'
        document['density_sha256']=sha256(array)
        _write(folder/'lut.json',document)
        progress('Optical LUT complete · publishing source-bound Registry artifacts')
        artifacts=[service.registry.store_file(array,array.name,'application/octet-stream' if store is not None else 'application/x-npz'),
                   service.registry.store_blob(canonical(document).encode(),'lens-psf-lut.json','application/json')]
        return dict(summary=document,artifacts=artifacts)


def get(service,identity):
    job=service.registry.get_run(identity)
    if job.get('job_kind')!=JOB_KIND: raise ValueError('Select a Lens optical PSF LUT job')
    if job['status']=='running':
        from .design_workshop_storage import root
        cache_id=digest(dict(specification={k:v for k,v in job['specification'].items() if k!='name'},
                             binding=job['pinned_source']))
        progress=root(service)/'lens-psf-luts'/cache_id/'progress.json'
        if progress.exists():
            report=json.loads(progress.read_text())
            if report.get('run_id')==identity: job['current_progress']=report
    if job['status']=='succeeded':
        summary=job['result']['summary']; artifacts=job['result']['artifacts']
        if (summary.get('specification')!=job['specification']
                or summary.get('pinned_source')!=job['pinned_source']
                or [a['name'] for a in artifacts] not in [['optical-intensity.npz','lens-psf-lut.json'],['optical-intensity.npy','lens-psf-lut.json']]
                or summary['density_sha256']!=artifacts[0]['sha256']):
            raise IntegrityError('LUT result differs from frozen job')
        # Verify source manifest as well as Registry's immutable completion binding.
        path=service.registry.blob_path(artifacts[1]['sha256'])
        if json.loads(path.read_bytes())!=summary: raise IntegrityError('LUT manifest was modified')
    return job


def load(service,identity):
    job=get(service,identity)
    if job['status']!='succeeded': raise ValueError('LUT generation has not completed')
    a=job['result']['artifacts'][0]
    binding=service.registry.artifact_binding(a['sha256'],owner_kind='runs',owner_id=identity)
    if a['name'].endswith('.npy'): density=np.load(binding['path'],allow_pickle=False,mmap_mode='r')
    else:
        with np.load(binding['path'],allow_pickle=False) as z: density=z['density_per_um2']
    return job['result']['summary'],density


def sample(service,identity,payload):
    if not isinstance(payload,dict) or set(payload)-{'angle_deg_xy','wavelength_nm','pixel_pitch_um','kernel_size','source_offset_um_xy'}:
        raise ValueError('Use explicit field, wavelength and sensor integration parameters')
    document,density=load(service,identity)
    angles=np.asarray(payload.get('angle_deg_xy'),float)
    if angles.shape!=(2,) or not np.isfinite(angles).all() or np.max(np.abs(angles))>=90:
        raise ValueError('Finite x/y field angles inside90deg required')
    plane,interpolation=lut.interpolate(document,density,np.tan(np.radians(angles)),payload.get('wavelength_nm',550.))
    kernel,diagnostic=lut.integrate_pixel(plane,document['optical_sample_um'],payload.get('pixel_pitch_um',2.9),
                   payload.get('kernel_size',129),payload.get('source_offset_um_xy',[0,0]))
    buffer=io.BytesIO();np.savez_compressed(buffer,kernel=kernel,optical_intensity_per_um2=plane)
    stride=max(1,math.ceil(plane.shape[0]/257))
    indices=np.arange((plane.shape[0]//2)%stride,plane.shape[0],stride)
    return dict(lut_id=document['lut_id'],source_run_id=identity,optical_density_sha256=document['density_sha256'],
                interpolation=interpolation,sensor_integration=diagnostic,ray_tracing_calls=0,
                optical_preview=plane[np.ix_(indices,indices)].tolist(),
                optical_preview_spacing_um=document['optical_sample_um']*stride,
                optical_preview_scope='Strided point display only; archive keeps full optical grid.',
                kernel=kernel.tolist()),buffer.getvalue()


def list_records(service):
    # Full adaptive node diagnostics belong to the per-run manifest. Avoid
    # transferring every multi-thousand-node manifest just to fill a selector.
    keys=('schema','lut_id','status','specification','statistics',
          'field_interpolation_qualified','unresolved_cells','density_sha256')
    return [dict(id=j['id'],name=j['name'],status=j['status'],error=j.get('error'),
            summary={k:v for k,v in ((j.get('result') or {}).get('summary') or {}).items() if k in keys})
            for j in service.registry.list_runs(job_kinds={JOB_KIND})]


def stream_bundle(files):
    """ZIP64 without buffering a multi-GB optical library in process memory."""
    import hashlib
    import zipfile
    class Sink:
        def __init__(self): self.offset=0; self.chunks=[]
        def tell(self): return self.offset
        def write(self,data):
            self.offset+=len(data)
            if data: self.chunks.append(data)
            return len(data)
        def flush(self): pass
        def drain(self):
            chunks=self.chunks;self.chunks=[]
            return chunks
    sink=Sink()
    with zipfile.ZipFile(sink,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
        for path,name,expected in files:
            sha=hashlib.sha256()
            with archive.open(name,'w',force_zip64=True) as member,Path(path).open('rb') as source:
                while block:=source.read(1024*1024):
                    sha.update(block);member.write(block)
                    yield from sink.drain()
                if sha.hexdigest()!=expected:
                    raise IntegrityError('LUT artifact changed during download')
            yield from sink.drain()
    yield from sink.drain()


def install_routes(app,service):
    from fastapi.responses import Response
    @app.get(ROUTE+'/template',tags=['lens_psf_lut'])
    def template():
        from .db_design_optics import SOURCES
        return dict(specification=lut.defaults(),sources=[dict(key=k,label=v['name'],revision_id=v['revision_id'])
                    for k,v in SOURCES.items() if not v.get('recipe_only')])
    @app.post(ROUTE,status_code=202,tags=['lens_psf_lut'])
    def create(payload:dict): return submit(service,payload)
    @app.get(ROUTE,tags=['lens_psf_lut'])
    def listing(): return list_records(service)
    @app.get(ROUTE+'/{identity}',tags=['lens_psf_lut'])
    def detail(identity:str): return get(service,identity)
    @app.post(ROUTE+'/{identity}/cancel',tags=['lens_psf_lut'])
    def cancel(identity:str):
        get(service,identity);return service.registry.cancel(identity)
    @app.post(ROUTE+'/{identity}/sample',tags=['lens_psf_lut'])
    def sampling(identity:str,payload:dict): return sample(service,identity,payload)[0]
    @app.post(ROUTE+'/{identity}/sample.npz',tags=['lens_psf_lut'])
    def sample_archive(identity:str,payload:dict):
        return Response(sample(service,identity,payload)[1],media_type='application/x-npz')
    @app.get(ROUTE+'/{identity}/bundle',tags=['lens_psf_lut'])
    def bundle(identity:str):
        from fastapi.responses import StreamingResponse
        j=get(service,identity)
        if j['status']!='succeeded': raise ValueError('LUT is not complete')
        files=[]
        for a in j['result']['artifacts']:
            p=service.registry.artifact_binding(a['sha256'],owner_kind='runs',owner_id=identity)['path']
            files.append((p,a['name'],a['sha256']))
        return StreamingResponse(stream_bundle(files),media_type='application/zip',headers={
                'Content-Disposition':'attachment; filename="lens-psf-lut.zip"'})

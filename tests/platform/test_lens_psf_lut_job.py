"""Real Registry lifecycle and hash-bound cache reuse with a bounded fake solver."""
from types import SimpleNamespace

import numpy as np
import pytest

from camera_platform import lens_psf_lut as lut, lens_psf_lut_job as jobs
from camera_platform.registry import SQLiteRegistry, IntegrityError


@pytest.fixture
def service(tmp_path,monkeypatch):
    from camera_platform import service as platform
    monkeypatch.setenv('CAMERA_PLATFORM_WORKSHOP_ROOT',str(tmp_path/'ssd'))
    monkeypatch.setattr(platform,'environment_fingerprint',lambda:{'test':True})
    monkeypatch.setattr(jobs,'binding',lambda workspace,spec:{'source':'pinned-test-source'})
    class Solver:
        trace_count=0
        def __init__(self,workspace,spec):self.spec=spec
        def solve(self,uv,progress):
            self.trace_count+=1
            a=np.zeros((1,65,65),np.float32);a[0,32,32]=4
            return a,dict(chief_sensor_xy_mm=list(uv))
    monkeypatch.setattr(lut,'PatentSolver',Solver)
    return SimpleNamespace(workspace=tmp_path,registry=SQLiteRegistry(tmp_path/'registry'),
                           loaded_environment={'test':True})


def run(service,spec):
    job=jobs.submit(service,spec);claimed=service.registry.claim()
    assert claimed['id']==job['id']
    result=jobs.execute(service,claimed)
    service.registry.finish(job['id'],result=result)
    return jobs.get(service,job['id'])


def specification():
    return lut.specification(dict(wavelength_nm=[550.],optical_size=65,
           max_field_nodes=25,initial_grid_xy=[2,2],half_field_deg_xy=[1.,1.]))


def test_generate_registry_reopen_sample_and_reuse_without_solver(service,monkeypatch):
    first=run(service,specification());d=first['result']['summary']
    assert d['status']=='qualified' and d['statistics']['computed_nodes']==4
    assert d['statistics']['reused_nodes']==5  # exact centered-pupil reflection
    assert not d['sensor_pixel_aperture_applied']
    restored=SimpleNamespace(**{**vars(service),'registry':SQLiteRegistry(service.registry.directory)})
    result,_=jobs.sample(restored,first['id'],dict(angle_deg_xy=[.25,.5],wavelength_nm=550.,
                         pixel_pitch_um=2.9,kernel_size=17))
    assert result['ray_tracing_calls']==0 and result['interpolation']['method']=='qualified_bilinear'
    assert result['sensor_integration']['pixel_aperture_applications']==1
    def forbidden(*args):raise AssertionError('Cache reuse must not instantiate a ray tracer')
    monkeypatch.setattr(lut,'PatentSolver',forbidden)
    second=run(restored,{**specification(),'name':'Second generation from cached nodes'})
    assert second['result']['summary']['lut_id']==d['lut_id']
    assert second['result']['summary']['density_sha256']==d['density_sha256']
    assert second['result']['summary']['statistics']['computed_nodes']==0
    assert second['result']['summary']['statistics']['reused_nodes']==9


def test_disk_backed_library_is_identical_and_reopens_lazily(service,monkeypatch):
    monkeypatch.setattr(jobs,'DISK_BACKED_THRESHOLD_BYTES',1)
    job=run(service,specification())
    d,a=jobs.load(service,job['id'])
    assert d['storage_format']=='npy_memmap' and isinstance(a,np.memmap)
    assert a.shape==(9,1,65,65) and np.all(a[:,:,32,32]==4)
    assert not list((service.workspace/'ssd').rglob('density-work.npy'))
    _,m=lut.interpolate(d,a,[.003,.004],550.)
    assert m['method']=='qualified_bilinear'


def test_same_size_modified_raw_lut_is_refused_before_reuse(service):
    job=run(service,specification())
    artifact=job['result']['artifacts'][0]
    path=service.registry.blob_path(artifact['sha256'])
    blob=bytearray(path.read_bytes());blob[len(blob)//2]^=1;path.write_bytes(blob)
    with pytest.raises(IntegrityError):jobs.load(service,job['id'])


def test_hash_changed_resumable_node_is_not_recomputed_silently(service):
    run(service,specification())
    cache=next((service.workspace/'ssd'/'lens-psf-lut-nodes').iterdir())
    node=next(p for p in cache.glob('*.npz') if p.name!='optical-intensity.npz')
    data=bytearray(node.read_bytes());data[-5]^=1;node.write_bytes(data)
    j=jobs.submit(service,{**specification(),'name':'Corrupt cache test'})
    with pytest.raises(IntegrityError):jobs.execute(service,service.registry.claim())


def test_large_archive_streams_zip64_and_checks_contents(tmp_path):
    import hashlib,io,zipfile
    p=tmp_path/'optical.npy';data=b'PSF'*1_000_000;p.write_bytes(data)
    chunks=list(jobs.stream_bundle([(p,p.name,hashlib.sha256(data).hexdigest())]))
    assert max(map(len,chunks))<=1024*1024
    with zipfile.ZipFile(io.BytesIO(b''.join(chunks))) as archive:
        assert archive.read(p.name)==data
    with pytest.raises(IntegrityError):
        list(jobs.stream_bundle([(p,p.name,'0'*64)]))

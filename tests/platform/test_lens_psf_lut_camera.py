import math
import numpy as np
import pytest
from fastapi.testclient import TestClient
from camera_platform import lens_psf_lut as lut,lens_psf_lut_camera as camera
from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient


class LinearLens:
    spec=lut.specification(dict(half_field_deg_xy=[6.51,3.7]))
    def chief(self,uv):
        radius=np.linalg.norm(uv);az=math.atan2(uv[1],uv[0]) if radius else math.pi/2
        angle=az-math.pi/2
        rot=np.array([[math.cos(angle),-math.sin(angle)],[math.sin(angle),math.cos(angle)]])
        return math.degrees(math.atan(radius)),np.array([0.,24.72*radius]),math.degrees(az),rot


def test_native_field_positions_preserve_sensor_extent_and_cfa_phase():
    fields=camera.sensor_fields(LinearLens(),128)
    assert len(fields)==9
    for f in fields:
        row,col=f['crop_origin_row_col']
        assert row%2==col%2==0 and row+128<=1097 and col+128<=1937
        np.testing.assert_allclose(np.asarray(f['uv'])*24.72,f['sensor_xy_mm'],atol=1e-9)
    coverage=camera.sensor_coverage(LinearLens())
    assert coverage['required_full_angle_deg_xy'][0]<13.02
    bad=LinearLens();bad.spec={**bad.spec,'half_field_deg_xy':[3.,2.]}
    with pytest.raises(ValueError,match='perimeter'):camera.sensor_coverage(bad)


def test_camera_submission_common_sdk_wait_and_cancel(tmp_path,monkeypatch):
    app=create_app(tmp_path/'state',tmp_path,start_worker=False,seed_demo=False)
    parent=dict(status='succeeded',result=dict(summary=dict(field_interpolation_qualified=True,
        specification=dict(wavelength_nm=[450.,550.,650.]),density_sha256='a'*64,lut_id='pinned')))
    monkeypatch.setattr(camera.library,'get',lambda *a:parent)
    with TestClient(app) as http:
        http.headers['X-Platform-Token']=http.get('/api/v1/session').json()['token']
        client=object.__new__(PlatformClient);client.http=http
        j=client.submit_lens_psf_camera('run-source',exposure_ms=4.)
        assert j['job_kind']==camera.JOB_KIND
        status=client.job_status(j['id'],expected_kind=camera.JOB_KIND)
        assert status['detail_path']=='lens-psf-cameras/'+j['id']
        assert http.post('/api/v1/lens-psf-cameras/'+j['id']+'/cancel',json={}).status_code==200
        result=client.wait(j['id'],timeout=1,expected_kind=camera.JOB_KIND)
        assert result['status']=='cancelled'
        parent['result']['summary']['field_interpolation_qualified']=False
        assert http.post('/api/v1/lens-psf-cameras',json={'lut_run_id':'run-partial'}).status_code==422

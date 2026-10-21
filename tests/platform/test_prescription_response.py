"""Independent response/reference checks, source identity and separate clipping."""

from copy import deepcopy
from pathlib import Path
import hashlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from run_camera_prescription_comparison import reference_photo, reference_raw
from camera_platform import electrical, spatial
from camera_platform.isp import default_profile
from camera_platform.prescription_response import ArchivedPrescription, require_same_design_scene
from camera_platform.response_chain import evaluate_response


@pytest.fixture
def request_data():
    req = spatial.request_template()
    for key in ("exposure_s", "frames", "noise_enabled"):
        req.pop(key)
    wave = [500,600]
    req.update(wavelength_nm=wave, pixel_pitch_um=3.0, qe=[[.2,.2],[.5,.5],[.8,.8]])
    req["lens"] = {"wavelength_nm":wave,"field_radius":[0,1],"theta_deg":[0,20],
        "angular_weights":[[[1,0],[1,0]]]*2,"relative_illumination":[[1,1]]*2,
        "field_reference_radius_mm":1.0}
    req["sensor_angular_response"]={"theta_deg":[0,20],"relative_response":[[[1,1]]*2]*3}
    image=np.full((8,8,2),1e-4)
    req["scene"].update(wavelength_nm=wave,spectral_irradiance=image,
        sampling={"kind":"native_sensor","full_sensor_shape":[128,128],"crop_origin":[59,61],"pixel_pitch_um":3.0})
    kernels=np.zeros((2,2,2,3,3));kernels[...,1,1]=1
    req["psf"].update(schema="field_intensity_psf_v1",wavelength_nm=wave,values=kernels.tolist(),
        sample_pitch_um=3.0,spatial_model="field_grid",field_y_mm=[-1,1],field_x_mm=[-1,1],
        field_coordinates="sensor_center_yx",interpolation="bilinear_source_position")
    return req


def test_direct_reference_matches_closed_form_area_spectrum_and_native_origin(request_data):
    photo, blurred=reference_photo(request_data,.004)
    photons=1e-4*(500+600)*1e-9/(6.62607015e-34*299792458)*100/2
    sites=np.array([[0,1],[1,2]])[(np.arange(8)[:,None]+59)%2,(np.arange(8)[None,:]+61)%2]
    expected=photons*(3e-6)**2*.004*np.array([.2,.5,.8])[sites]
    np.testing.assert_allclose(photo,expected,rtol=2e-15)
    np.testing.assert_allclose(blurred,request_data["scene"]["spectral_irradiance"],rtol=2e-15)


def test_asymmetric_field_psf_source_weighting_and_independent_adc(request_data):
    req=deepcopy(request_data)
    req["scene"]["spectral_irradiance"][:,4:]*=7
    kernels=np.asarray(req["psf"]["values"])
    kernels[:,1,1,1,1]=.4;kernels[:,1,1,1,2]=.6
    req["psf"]["values"]=kernels.tolist()
    reference,blur=reference_photo(req,.004)
    model=electrical.default_electrical_model(req["readout"])
    out=evaluate_response(req,model,exposure_s=.004,analog_gain=2,noise_enabled=False)
    np.testing.assert_allclose(out["arrays"]["blurred_spectral_irradiance"],blur,rtol=1e-12,atol=1e-18)
    np.testing.assert_allclose(out["arrays"]["mean_photocharge_e"],reference,rtol=1e-12)
    np.testing.assert_array_equal(out["arrays"]["raw_dn"],reference_raw(reference,model,.004,2))


def test_quarter_throughput_is_not_canceled_by_gain_or_renormalized(request_data):
    model=electrical.default_electrical_model(request_data["readout"])
    profile=default_profile();profile["tone"]["curve"]="linear"
    clear=evaluate_response(request_data,model,exposure_s=.004,analog_gain=1,isp_profile=profile)
    quarter=deepcopy(request_data);quarter["scene"]["spectral_irradiance"]*=.25
    fixed=evaluate_response(quarter,model,exposure_s=.004,analog_gain=1,isp_profile=profile)
    compensated=evaluate_response(quarter,model,exposure_s=.004,analog_gain=4,isp_profile=profile)
    np.testing.assert_allclose(compensated["arrays"]["mean_photocharge_e"],.25*clear["arrays"]["mean_photocharge_e"],rtol=1e-13)
    assert fixed["arrays"]["rgb"].mean() < clear["arrays"]["rgb"].mean()
    # Same brightness can hide four times fewer photoelectrons.
    assert np.max(abs(compensated["arrays"]["raw_dn"]-clear["arrays"]["raw_dn"]))<=1
    assert np.max(abs(compensated["arrays"]["rgb"]-clear["arrays"]["rgb"]))<=1/(4095-64)


def test_readout_upper_rail_can_precede_well_capacity():
    model=electrical.default_electrical_model({"full_well_e":10000,"read_noise_e":0,
        "dark_current_e_per_s":0,"gain":1,"bit_depth":12,"black_level_dn":64})
    photo=np.array([[100,6000],[8000,10000]],dtype=float)
    a=reference_raw(photo,model,.004,1);b=reference_raw(photo,model,.004,2)
    assert a[0,1]<4095 and b[0,1]==4095
    assert photo[0,1]<model["full_well_e"]


def case_stub(transmission=1.0):
    obj=SimpleNamespace(prescription={"surfaces":[{"radius":20}],"stop_transmission":{"T":transmission}},
        design_asset_id="same-design",request={"lens":{"radiometry":{"irradiance_per_radiance_sr":[[.002*transmission],[.003*transmission]]}},
        "scene":{"spectral_irradiance":np.ones((8,8,2))*np.array([.002,.003])*transmission}})
    obj.identity=lambda:{"sensor_payload_sha256":"sensor","scene_render_id":"scene","scene_render_content_hash":"source", "sampling":{"full_sensor_shape":[128,128],"crop_origin":[56,56]}}
    return obj


def test_same_radiance_different_projection_is_allowed_but_scene_change_is_not():
    a,b=case_stub(),case_stub(.25)
    require_same_design_scene([a,b])
    b.request["scene"]["spectral_irradiance"][0,0,0]*=2
    with pytest.raises(ValueError,match="radiance"):
        require_same_design_scene([a,b])


def test_stop_comparison_rejects_geometry_substitution():
    a,b=case_stub(),case_stub(.25)
    b.prescription["surfaces"][0]["radius"]=30
    with pytest.raises(ValueError,match="geometry"):
        require_same_design_scene([a,b])


def test_blob_hash_and_read_only_source_check(tmp_path):
    archive=ArchivedPrescription.__new__(ArchivedPrescription)
    archive.blobs=tmp_path;archive.read_hashes={}
    content=b"original evidence";checksum=hashlib.sha256(content).hexdigest()
    path=tmp_path/checksum;path.write_bytes(content)
    item={"name":"input.json","sha256":checksum,"bytes":len(content)}
    assert archive.content(item)==content
    archive.assert_unchanged()
    path.write_bytes(b"changed")
    with pytest.raises(ValueError,match="changed"):
        archive.assert_unchanged()

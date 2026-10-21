"""A source-bound mode must constrain physical exposure before any RAW exists."""
import copy

import pytest

from camera_platform import db_design_sensor_modes as modes
from camera_platform import db_design_sensor_timing as timing
from camera_platform import db_design_job as jobs


def test_manufacturer_mode_matches_acquired_register_clock_and_controls():
    m = modes.mode('kurokesu_1080p30_raw10')
    assert m['hmax'] == 4400 and m['timing_clock_hz'] == 148_500_000
    assert m['vmax'] == 1125
    assert m['line_time_us'] == pytest.approx(29.62962962962963)
    assert m['frame_period_ms'] == pytest.approx(1000/30)
    assert m['requested_exposure_min_us'] == 30
    assert m['requested_exposure_max_us'] == 33274
    assert m['source_output_bit_depth'] == 10
    assert m['classification'] == 'module_manufacturer_driver_derived'


def test_fps_changes_vmax_not_line_period_and_retains_driver_control_limit():
    fast = modes.mode(modes.IDS[0],30)
    slow = modes.mode(modes.IDS[0],15)
    assert slow['vmax'] == 2250
    assert slow['line_time_us'] == fast['line_time_us']
    assert slow['frame_period_ms'] == pytest.approx(2*fast['frame_period_ms'])
    # This module's driver exposes a fixed exposure cap even at a slower FPS.
    assert modes.exposure(slow,40)['status'] == 'blocked'


@pytest.mark.parametrize('fps',[0,1,30.1,60,float('nan'),True])
def test_out_of_driver_range_fps_is_rejected(fps):
    with pytest.raises(ValueError): modes.mode(modes.IDS[0],fps)


def test_exposure_quantization_and_fixed_frame_rejection():
    m = modes.mode(modes.IDS[0])
    r = modes.exposure(m,1)
    assert r['coarse_integration_lines'] == 34
    assert r['shs1_register'] == 1090
    assert r['actual_exposure_ms'] == pytest.approx(1.0074074074074073)
    assert modes.exposure(m,.01)['status'] == 'blocked'
    assert modes.exposure(m,34)['actual_exposure_ms'] is None
    assert modes.exposure(m,34)['shs1_register'] is None


def test_missing_high_fps_registers_stay_assumed_and_long_exposure_has_no_capture():
    m = modes.mode('assumed_1080p120')
    assert m['classification'] == 'authored_timing_assumption'
    assert m['source_commit'] is None
    rows = modes.schedule(m,[1,4,16])
    assert [r['status'] for r in rows] == ['eligible','eligible','blocked']
    assert rows[-1]['actual_exposure_ms'] is None
    assert rows[-1]['frame_period_ms'] == pytest.approx(1000/120)
    assert modes.exposure(modes.mode('assumed_1080p60'),16)['status'] == 'eligible'


def test_no_fitting_exposure_or_aliased_coarse_requests_are_rejected():
    with pytest.raises(ValueError): modes.schedule(modes.mode(modes.IDS[2]),[16,32])
    with pytest.raises(ValueError): modes.schedule(modes.mode(modes.IDS[0]),[1,1.001])


def test_modified_mode_is_not_a_trusted_control_contract():
    m = modes.mode(modes.IDS[0]);m['requested_exposure_max_us']=1e9
    with pytest.raises(ValueError): modes.exposure(m,1000)


def test_matched_global_control_uses_identical_actual_exposure_and_cadence():
    m = modes.mode(modes.IDS[0]);e=modes.exposure(m,1)
    base={'line_time_us':0.,'row_exposure_start_s':[-.0005,.0005]}
    gs=modes.apply_clock(base,m,e)
    rs=modes.apply_clock({**base,'line_time_us':m['line_time_us']},m,e)
    assert gs['actual_exposure_ms']==rs['actual_exposure_ms']==e['actual_exposure_ms']
    assert gs['frame_cadence_s']==rs['frame_cadence_s']==pytest.approx(1/30)
    assert gs['row_exposure_next_frame_start_s'][0]==pytest.approx(1/30-.0005)


def test_job_mode_fixes_line_clock_and_keeps_legacy_explicit_assumptions():
    base={'comparison_mode':'sensor_shutter','source_run_id':'run-'+'a'*32}
    assert jobs.specification(base)['line_time_us']==[0,10,20]
    r=jobs.specification({**base,'operating_mode_id':modes.IDS[0],'fps':15})
    assert r['line_time_us']==[0,modes.mode(modes.IDS[0])['line_time_us']]
    assert jobs.specification(r)==r
    with pytest.raises(ValueError): jobs.specification({**r,'line_time_us':[0,20]})
    with pytest.raises(ValueError): jobs.specification({**base,'fps':30})

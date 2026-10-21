"""Source provenance and located crops on a single full-array row clock."""
import copy
import hashlib
import json
import sqlite3

import numpy as np
import pytest

from camera_platform import db_design_sensor_timing as timing


@pytest.fixture
def source(tmp_path, monkeypatch):
    folder = tmp_path / timing.SOURCE_DIRECTORY
    folder.mkdir(parents=True)
    pdf = b'bounded test source bytes'
    pdf_hash = hashlib.sha256(pdf).hexdigest()
    (folder / 'sony-imx462-family-flyer.pdf').write_bytes(pdf)
    record = json.dumps({'source_url': timing.SOURCE_URL, 'sha256': pdf_hash,
                         'variant': 'IMX462LQR Ver2.0'}).encode()
    (folder / 'sony-imx462-family-flyer.source.json').write_bytes(record)
    monkeypatch.setattr(timing, 'PDF_SHA', pdf_hash)
    monkeypatch.setattr(timing, 'SOURCE_RECORD_SHA', hashlib.sha256(record).hexdigest())
    snapshot = json.dumps({'metadata': {'code': 'DEF-2011-804'}})
    snapshot_hash = hashlib.sha256(snapshot.encode()).hexdigest()
    monkeypatch.setattr(timing, 'REVISION_SHA', snapshot_hash)
    with sqlite3.connect(tmp_path / 'camerae2e_db/catalog.sqlite') as db:
        db.execute('CREATE TABLE model_revisions(revision_id,component_id,content_sha256,snapshot_json)')
        db.execute('CREATE TABLE sensor_geometry(revision_id,pixel_pitch_um,width_px,height_px,active_width_mm,active_height_mm)')
        db.execute('INSERT INTO model_revisions VALUES (?,?,?,?)', (timing.REVISION, 'sensor', snapshot_hash, snapshot))
        db.execute('INSERT INTO sensor_geometry VALUES (?,2.9,NULL,NULL,NULL,NULL)', (timing.REVISION,))
    return tmp_path


def test_new_vendor_counts_do_not_overwrite_legacy_missing_values(source):
    before = (source / 'camerae2e_db/catalog.sqlite').read_bytes()
    result = timing.profile(source)
    assert result['total_shape_row_col'] == [1109, 1945]
    assert result['effective_shape_row_col'] == [1097, 1945]
    assert result['active_shape_row_col'] == [1097, 1937]
    assert result['recording_shape_row_col'] == [1080, 1920]
    assert result['dimension_source']['classification'] == 'newly_acquired_official_vendor_document'
    assert result['legacy_catalog']['dimensions_before_enrichment']['height_px'] is None
    assert result['legacy_catalog']['catalog_modified'] is False
    assert 'line_time_us' in result['assumptions'] and 'verified_line_time' in result['missing_product_information']
    assert (source / 'camerae2e_db/catalog.sqlite').read_bytes() == before


@pytest.mark.parametrize('kind', ['pdf', 'record', 'snapshot', 'legacy_dimensions'])
def test_changed_sources_are_rejected_without_silent_new_defaults(source, kind):
    if kind in ('pdf', 'record'):
        name = 'sony-imx462-family-flyer.pdf' if kind == 'pdf' else 'sony-imx462-family-flyer.source.json'
        path = source / timing.SOURCE_DIRECTORY / name
        data = path.read_bytes()
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    else:
        with sqlite3.connect(source / 'camerae2e_db/catalog.sqlite') as db:
            if kind == 'snapshot':
                db.execute("UPDATE model_revisions SET snapshot_json='{}'")
            else:
                db.execute('UPDATE sensor_geometry SET height_px=1080')
    with pytest.raises(ValueError):
        timing.profile(source)


def test_located_even_cfa_crops_and_optical_halo_fit_vendor_active_area(source):
    profile = timing.profile(source)
    for field in profile['roi_layout']:
        r, c = field['origin_row_col']
        assert r % 2 == c % 2 == 0
        assert r >= 64 and c >= 64
        assert r + 256 + 64 <= 1097 and c + 256 + 64 <= 1937
        y, x = field['center_active_row_col']
        np.testing.assert_allclose(field['image_xy_mm'], [(x - 968) * .0029, (548 - y) * .0029], atol=1e-15)
    np.testing.assert_allclose(profile['roi_layout'][0]['image_xy_mm'], [-.00145, .00145])
    np.testing.assert_allclose(profile['roi_layout'][1]['image_xy_mm'], [2.24895, 1.03385])


def test_optical_anchor_is_separate_from_geometric_roi_center_and_does_not_recenter_clock(source):
    profile = timing.profile(source)
    center, outer = profile['roi_layout']
    assert center['center_active_row_col'] == [547.5, 967.5]
    assert center['image_xy_mm'] == center['roi_center_image_xy_mm'] == [-.00145, .00145]
    assert center['optical_anchor_active_row_col'] == [548., 968.]
    assert center['optical_anchor_local_row_col'] == [128., 128.]
    assert center['optical_anchor_image_xy_mm'] == [0., 0.]
    assert outer['optical_anchor_active_row_col'] == outer['center_active_row_col']
    assert outer['optical_anchor_local_row_col'] == [127.5, 127.5]
    assert outer['optical_anchor_image_xy_mm'] == outer['image_xy_mm']
    clock = timing.field_timing(profile, 'center', 20, 4)
    assert clock['roi_center_mid_s'] == pytest.approx(-.00001)
    assert clock['row_exposure_mid_s'][128] == 0
    assert timing.field_timing(profile, 'outer-ne', 20, 4)['roi_center_mid_s'] == pytest.approx(-.00713)


def test_periphery_uses_same_global_clock_and_is_not_recentered(source):
    profile = timing.profile(source)
    center = timing.field_timing(profile, 'center', 20, 4)
    outer = timing.field_timing(profile, 'outer-ne', 20, 4)
    assert center['active_row_indices'][0] == 420 and outer['active_row_indices'][0] == 64
    assert center['total_row_indices'][0] == 432 and outer['total_row_indices'][0] == 76
    assert center['roi_center_mid_s'] == pytest.approx(-.00001)
    assert outer['roi_center_mid_s'] == pytest.approx(-.00713)
    np.testing.assert_allclose(np.asarray(outer['row_exposure_mid_s']) - center['row_exposure_mid_s'], -.00712, atol=1e-17)
    assert outer['local_roi_scan_ms'] == pytest.approx(5.1)
    assert outer['active_scan_ms'] == pytest.approx(21.92)
    assert outer['total_scan_ms'] == pytest.approx(22.16)
    assert outer['total_row0_mid_s'] == pytest.approx(-.0112)
    assert outer['total_last_row_mid_s'] == pytest.approx(.01096)
    assert outer['total_row0_exposure_start_s'] == pytest.approx(-.0132)
    assert outer['frame_cadence_s'] is None
    np.testing.assert_allclose(np.asarray(outer['row_exposure_end_s']) - outer['row_exposure_start_s'], .004)


def test_global_baseline_and_exposure_axis_share_reference_time(source):
    profile = timing.profile(source)
    for field in ('center', 'outer-ne'):
        gs = timing.field_timing(profile, field, 0, 4)
        assert set(gs['row_exposure_mid_s']) == {0.}
        assert set(gs['row_exposure_start_s']) == {-.002}
        assert gs['shutter_mode'] == 'global' and gs['total_scan_ms'] == 0
    a = timing.field_timing(profile, 'outer-ne', 10, 1)
    b = timing.field_timing(profile, 'outer-ne', 10, 16)
    assert a['row_exposure_mid_s'] == b['row_exposure_mid_s']
    np.testing.assert_allclose(np.diff(b['row_exposure_mid_s']), 10e-6, atol=1e-17)


def test_profile_mutation_and_unknown_roi_cannot_change_clock_silently(source):
    profile = timing.profile(source)
    changed = copy.deepcopy(profile)
    changed['roi_layout'][1]['origin_row_col'][0] += 2
    with pytest.raises(ValueError, match='bound hash'):
        timing.field_timing(changed, 'outer-ne', 10, 4)
    with pytest.raises(ValueError, match='Unknown sensor ROI'):
        timing.field_timing(profile, 'arbitrary', 10, 4)


@pytest.mark.parametrize('axis', [[10, 20], [0], [0, 10, 10], [0, True], [0, float('nan')], [0, 101], [0, -1], [0, .01]])
def test_unsupported_line_time_axes_are_rejected(axis):
    with pytest.raises(ValueError, match='line_time_us'):
        timing.validate_line_times(axis)


def test_fractional_line_times_remain_distinct():
    assert timing.validate_line_times([0, 10.000001, 10.000002]) == [0., 10.000001, 10.000002]


def test_field_clock_rejects_positive_line_time_below_declared_mode_range(source):
    with pytest.raises(ValueError, match='supported native-row'):
        timing.field_timing(timing.profile(source), 'center', .01, 4)

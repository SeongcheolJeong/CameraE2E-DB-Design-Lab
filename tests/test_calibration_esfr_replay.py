from copy import deepcopy

import pytest

from camera_platform.calibration_esfr_replay import adapted_job
from camera_platform.registry import IntegrityError, digest


def fixture(count=112):
    rois, nuisance, measurements, nodes = [], {}, [], []
    for i in range(count):
        key = f"edge-{i}"
        box = [i * 2, 12, 62, 62]
        xy = [2 * (12 + 30.5) / 2559 - 1, 2 * (i * 2 + 30.5) / 1439 - 1]
        rois.append(dict(id=key, field_id=f"star-{i//8}", kind="edge", box=box))
        nuisance[key] = dict(geometry=dict(box=box, field_id=f"star-{i//8}"))
        measurements.append(dict(id=f"m-{i}", provenance=dict(roi_id=key, native_roi=box),
                                 observations=dict(field_xy=list(xy), orientation_deg=i % 180)))
        nodes.append(dict(field_xy=xy, sigma_x_px=1+i/1000, sigma_y_px=1+i/1000, theta_deg=0))
    source = dict(specification=dict(rois=rois), result=dict(summary=dict(candidate_profile=dict(
        fields={}, geometry=dict(roi_nuisance=nuisance), fit_assumptions=[]))))
    base = dict(blur=dict(field_nodes=[dict(field_xy=[0, 0], sigma_x_px=1.5, sigma_y_px=1.5)]))
    calibration = dict(measurement_snapshots=measurements, candidate_profile=dict(blur=dict(field_nodes=nodes)), base_profile=base)
    metadata = dict(geometry=dict(crop_origin=[0, 0], full_sensor_shape=[1440, 2560]), raw_format=dict(cfa="RGGB", bit_depth=12), pixel_pitch_um=2.9, conditions={})
    source["capture_snapshots"] = [dict(metadata=metadata)]
    for p in (base, calibration["candidate_profile"]):
        p.update(native_shape=[1440, 2560], cfa="RGGB", pixel_pitch_um=2.9, electronics=dict(bit_depth=12), conditions={})
    return source, calibration


def test_112_nodes_replayed_unchanged_with_development_geometry():
    source, calibration = fixture()
    originals = deepcopy((source, calibration))
    job, mapping = adapted_job(source, calibration)
    fields = job["frozen_profile_snapshot"]["fields"]
    assert len(fields) == len(mapping) == 112
    assert len({r["field_id"] for r in job["specification"]["rois"]}) == 112
    for i, node in enumerate(calibration["candidate_profile"]["blur"]["field_nodes"]):
        assert fields[f"edge-{i}"]["sigma_x_px"] == node["sigma_x_px"]
    assert job["frozen_profile_snapshot"]["calibration_profile_sha256"] == digest(calibration["candidate_profile"])
    assert (source, calibration) == originals


def test_no_nearest_field_or_partial_coverage_fallback():
    source, calibration = fixture()
    calibration["candidate_profile"]["blur"]["field_nodes"][0]["field_xy"][0] += 1e-9
    with pytest.raises(IntegrityError, match="exact"):
        adapted_job(source, calibration)
    source, calibration = fixture()
    calibration["measurement_snapshots"].pop()
    with pytest.raises(IntegrityError, match="every source edge"):
        adapted_job(source, calibration)


def test_roi_geometry_and_spatial_baseline_cannot_be_silently_changed():
    source, calibration = fixture()
    calibration["measurement_snapshots"][0]["provenance"]["native_roi"] = [0, 0, 62, 62]
    with pytest.raises(IntegrityError, match="native ROI"):
        adapted_job(source, calibration)
    source, calibration = fixture()
    calibration["base_profile"]["blur"]["field_nodes"][0]["sigma_y_px"] = 2
    with pytest.raises(ValueError, match="uniform isotropic"):
        adapted_job(source, calibration)


def forward_fixture():
    source, calibration = fixture(3)
    metadata = source['capture_snapshots'][0]['metadata']
    metadata.update(camera_id='camera-1', source_kind='synthetic')
    source['capture_snapshots'][0]['id'] = 'development-1'
    source['development_ids'] = ['development-1']
    frozen = deepcopy(source['result']['summary']['candidate_profile'])
    frozen.update({key: deepcopy(metadata[key]) for key in ('camera_id', 'conditions', 'raw_format', 'source_kind')})
    frozen['geometry'].update(deepcopy(metadata['geometry']))
    frozen['development_capture_ids'] = ['development-1']
    frozen['fields'] = {f'edge-{i}': {key: node[key] for key in ('sigma_x_px', 'sigma_y_px', 'theta_deg')}
                        for i, node in enumerate(calibration['candidate_profile']['blur']['field_nodes'])}
    calibration['candidate_profile']['esfr_forward_model'] = {
        'schema': 'camera_calibration_esfr_forward_binding_v1',
        'method': 'development_native_bayer_same_observer_gaussian_v1',
        'fit_data_role': 'development_only', 'profile': frozen, 'profile_sha256': digest(frozen)}
    return source, calibration


def test_forward_replay_uses_embedded_development_profile_not_old_source_fit():
    source, calibration = forward_fixture()
    before = deepcopy(calibration['candidate_profile']['esfr_forward_model']['profile'])
    # The former analytic source template is irrelevant once the forward
    # development-only profile has been frozen inside this candidate.
    source['result']['summary']['candidate_profile']['geometry']['roi_nuisance']['edge-0']['geometry']['box'] = [999,0,62,62]
    source['result']['summary']['candidate_profile']['fields'] = {'wrong': {}}
    result, mapping = adapted_job(source, calibration)
    assert len(mapping) == 3
    assert result['frozen_profile_snapshot']['scope'] == 'frozen_calibration_lab_forward_observer_image_remeasurement'
    assert result['frozen_profile_snapshot']['geometry']['roi_nuisance']['edge-0']['geometry']['box'] == before['geometry']['roi_nuisance']['edge-0']['geometry']['box']
    assert calibration['candidate_profile']['esfr_forward_model']['profile'] == before


@pytest.mark.parametrize('change', ['hash', 'width', 'missing_field', 'roi'])
def test_forward_profile_hash_fields_and_native_roi_are_guarded(change):
    source, calibration = forward_fixture()
    wrapper = calibration['candidate_profile']['esfr_forward_model']
    if change == 'hash':
        wrapper['profile_sha256'] = '0' * 64
    elif change == 'width':
        calibration['candidate_profile']['blur']['field_nodes'][0]['sigma_x_px'] += .001
    elif change == 'missing_field':
        wrapper['profile']['fields'].pop('edge-0')
        wrapper['profile_sha256'] = digest(wrapper['profile'])
    else:
        wrapper['profile']['geometry']['roi_nuisance']['edge-0']['geometry']['box'] = [1,12,62,62]
        wrapper['profile_sha256'] = digest(wrapper['profile'])
    with pytest.raises(IntegrityError):
        adapted_job(source, calibration)


@pytest.mark.parametrize('change', ['camera_id', 'conditions', 'raw_format', 'crop_origin', 'development_capture_ids'])
def test_recomputed_inner_hash_cannot_relabel_frozen_capture_context(change):
    source, calibration = forward_fixture()
    wrapper = calibration['candidate_profile']['esfr_forward_model']
    frozen = wrapper['profile']
    if change == 'crop_origin':
        frozen['geometry']['crop_origin'] = [2,0]
    elif change == 'conditions':
        frozen['conditions'] = {'exposure_s': 999}
    elif change == 'raw_format':
        frozen['raw_format']['cfa'] = 'BGGR'
    elif change == 'development_capture_ids':
        frozen['development_capture_ids'] = ['unrelated-capture']
    else:
        frozen[change] = 'other-camera'
    wrapper['profile_sha256'] = digest(frozen)
    with pytest.raises(IntegrityError):
        adapted_job(source, calibration)

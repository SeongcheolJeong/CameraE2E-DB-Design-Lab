"""Shared RAW response study evaluates the declared meter, preserving both paths."""
import io
import json
from copy import deepcopy

import numpy as np
import pytest
from test_electrical_response_study import finish_all
from test_hdr_workflow import load_array
from test_hdr_workflow import workflow as _hdr_workflow
from test_retained_charge_workflow import workflow as _retained_workflow

from camera_platform import electrical_response
from camera_platform import electrical_response_study as study
from camera_platform.registry import IntegrityError

hdr_workflow = _hdr_workflow
retained_workflow = _retained_workflow


@pytest.mark.parametrize('nonlinear', [False, True])
@pytest.mark.parametrize('readout', ['HCG', 'LCG'])
def test_shared_response_evaluates_only_fixed_selected_original_codes(
    retained_workflow, nonlinear, readout
):
    service, sdk, assembly, scene, control, *_ = retained_workflow
    draft = sdk.template(
        'capture-program', assembly_id=assembly['id'],
        control_sequence_id=control['id'],
        mode='electrical_control_' + readout.lower() + ('_nonlinear' if nonlinear else ''),
    )
    draft['payload']['electrical_control_policy']['target_black_relative_dn'] = {
        'minimum': 10, 'maximum': 20,
    }
    program = sdk.create_capture_program(draft)
    spec = study.template(service, assembly['id'], scene['id'], control['id'], program['id'])
    plan = study.plan(service, spec)
    record = study.submit(service, spec, plan['plan_hash'])
    finish_all(service)
    record = study.get(service, record['id'])
    index = ['HCG', 'LCG'].index(readout)
    for role in ('baseline', 'candidate'):
        observed = record['result']['summary']['cells'][0][role]
        child = service.registry.get_run(observed['run_id'])
        raw = load_array(service, child, 'raw_groups')
        assert raw.shape[1] == 2
        assert observed['observation_readout_id'] == readout
        assert observed['observation_readout_index'] == index
        assert observed['preview_readout_id'] == 'HCG'
        assert observed['raw_format'] == child['result']['summary']['raw_formats'][index]
        expected = electrical_response.evaluate(
            raw[:, index:index + 1], observed['raw_format'],
            observed['actual_timing'], spec['evaluation'],
        )
        assert observed['evaluation'] == expected
        evaluation = expected['specification']
        # Independent ROI/site mean from immutable original integer RAW.
        r, c = evaluation['roi']['origin']
        h, w = evaluation['roi']['shape']
        sites = np.array([['R', 'Gr'], ['Gb', 'B']])
        yy, xx = np.indices(raw.shape[-2:])
        mask = np.isin(sites[yy % 2, xx % 2][r:r+h, c:c+w], evaluation['sites'])
        for i, frame in enumerate(expected['frames']):
            if frame['status'] == 'observed':
                mean = raw[i, index, r:r+h, c:c+w][mask].astype(float).mean()
                mean -= observed['raw_format']['black_level_dn']
                assert frame['statistics']['value_dn'] == mean
    # Saved study reads and cached submissions preserve selected path identity.
    assert study.submit(service, spec, plan['plan_hash'])['id'] == record['id']
    assert study.get(service, record['id']) == record
    original = service.registry.get('capture_programs', program['id'])
    changed = deepcopy(draft)
    changed.update(name='Opposite fixed meter', revision='opposite')
    changed['payload']['observation_readout_id'] = 'LCG' if readout == 'HCG' else 'HCG'
    other = sdk.create_capture_program(changed)
    other_spec = {**spec, 'capture_program_id': other['id']}
    assert study.plan(service, other_spec)['plan_hash'] != plan['plan_hash']
    assert service.registry.get('capture_programs', program['id']) == original
    assert readout in json.dumps(record['result']['summary'])


def test_unobserved_raw_range_and_preview_path_cannot_misrepresent_paired_evidence(
    retained_workflow,
):
    service, sdk, assembly, scene, control, *_ = retained_workflow
    draft = sdk.template(
        'capture-program', assembly_id=assembly['id'], control_sequence_id=control['id'],
        mode='electrical_control_lcg',
    )
    program = sdk.create_capture_program(draft)
    spec = study.template(service, assembly['id'], scene['id'], control['id'], program['id'])
    plan = study.plan(service, spec)
    parent = study.submit(service, spec, plan['plan_hash'])
    finish_all(service)
    saved = study.get(service, parent['id'])
    observed = saved['result']['summary']['cells'][0]['candidate']
    child = service.registry.get_run(observed['run_id'])
    assert observed['observation_readout_id'] == 'LCG'
    # Keep selected LCG pixels and every timing/control field exact. Only the
    # unused HCG path exceeds its declared ADC range, in a valid fresh NPY blob.
    malformed = deepcopy(child)
    raw = load_array(service, child, 'raw_groups').copy()
    raw[0, 0, 0, 0] = 2 ** child['result']['summary']['raw_formats'][0]['bit_depth']
    content = io.BytesIO()
    np.save(content, raw, allow_pickle=False)
    old = next(a for a in child['artifacts'] if a['name'] == 'raw_groups.npy')
    replacement = service.registry.store_blob(content.getvalue(), old['name'], old['media_type'])
    replacement.update({key: value for key, value in old.items() if key not in replacement})
    malformed['artifacts'] = [
        replacement if a['name'] == old['name'] else a for a in malformed['artifacts']
    ]
    with pytest.raises(IntegrityError, match='ADC code range'):
        study._observation(service, malformed, spec['evaluation'])
    mislabeled = deepcopy(child)
    mislabeled['result']['summary']['preview_readout_id'] = 'LCG'
    with pytest.raises(IntegrityError, match='original HCG display path'):
        study._observation(service, mislabeled, spec['evaluation'])
    # Direct local-guard probes operate on detached records only. The persisted
    # actual child and its parent observation stay readable and unchanged.
    assert service.registry.get_run(child['id']) == child
    assert study.get(service, saved['id']) == saved

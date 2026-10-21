"""Source-bound optical structure studies. 2D diagnostics are never 3D QE LUTs."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np

from .registry import canonical, digest

SCHEMA = 'camera_sensor_structure_study_v1'
VARIANTS = ('reviewed', 'without_dti', 'ocl_shift')


def defaults():
    return dict(schema=SCHEMA, name='IMX662 DTI / OCL structure study',
                report_code='DEF-2206-805', dimension=2,
                variants=list(VARIANTS), wavelength_nm=[550.0], angles_deg=[0.0, 20.0],
                resolutions=[60, 80], neighborhood=5, guard_cells=1,
                after_source_time=60.0, pml_um=0.8, source_sigma_pitch=0.30,
                max_wall_seconds=7200, max_rss_gib=24.0)


def specification(value):
    if not isinstance(value, dict) or set(value) - set(defaults()):
        raise ValueError('Unknown structure study settings')
    s = {**defaults(), **copy.deepcopy(value)}
    if s['schema'] != SCHEMA or s['report_code'] != 'DEF-2206-805' or s['dimension'] not in (2, 3):
        raise ValueError('First optical structure adapter supports IMX662 in 2D or 3D')
    for key, low, high in [('wavelength_nm', 450, 650), ('angles_deg', -40, 40), ('resolutions', 30, 200)]:
        a = np.asarray(s[key], dtype=float)
        if a.ndim != 1 or not 1 <= len(a) <= 9 or not np.isfinite(a).all() or np.any((a < low) | (a > high)) or len(set(a)) != len(a):
            raise ValueError(f'Invalid {key}')
        s[key] = sorted(a.tolist())
    if len(s['resolutions']) < 2 or any(int(v) != v for v in s['resolutions']):
        raise ValueError('At least two integer resolutions required; no single-grid qualification')
    if not s['variants'] or len(set(s['variants'])) != len(s['variants']) or set(s['variants']) - set(VARIANTS):
        raise ValueError('Unknown or duplicated variant')
    for key, lo, hi in [('neighborhood', 3, 11), ('guard_cells', 1, 3), ('after_source_time', 20, 300),
                        ('pml_um', .5, 2), ('source_sigma_pitch', .15, .45),
                        ('max_wall_seconds', 1, 7200), ('max_rss_gib', .1, 24)]:
        v = s[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not np.isfinite(v) or not lo <= v <= hi:
            raise ValueError(f'Invalid {key}')
    if int(s['neighborhood']) != s['neighborhood'] or s['neighborhood'] % 2 != 1 or int(s['guard_cells']) != s['guard_cells']:
        raise ValueError('Odd integer neighborhood and integer guard required')
    return s


def source_bundle(workspace):
    from pyisetcam.physical_estimate import image_sensor_physical_model
    from pyisetcam.sensor_techinsights import _manifest
    base = Path(workspace)
    manifest, evidence_hash = _manifest()
    reviewed = manifest['profiles']['DEF-2206-805']
    model = image_sensor_physical_model('DEF-2206-805',
        root=base/'camerae2e_db/fdtd_tcad/image_sensor_db',
        optical_assumptions={'geometry_source': 'reviewed_techinsights'})
    observations = {**reviewed['selected_facts'], **reviewed['supplemental_observations']}
    def fact(name): return float(observations[name]['value'])
    catalog_path = base/'camerae2e_db/fdtd_tcad/image_sensor_db/sensor_catalog.json'
    catalog = json.loads(catalog_path.read_text())
    record = next(r for r in catalog['records'] if r['code'] == 'DEP-2206-801')
    geometry = dict(pitch_um=fact('pixel_pitch_um'), si_um=fact('active_si_thickness_um'),
        ocl_height_um=fact('microlens_height_um'), ocl_radius_um=fact('microlens_vertex_radius_um'),
        cfa_um=fact('green_cfa_thickness_um'), dti_depth_um=fact('active_pixel_dti_depth_um'),
        dti_width_um=fact('active_pixel_dti_average_width_um'),
        passivation_um=round(fact('optical_stack_height_um')-fact('microlens_height_um')-fact('green_cfa_thickness_um'), 12),
        # Shape inferred from height and vertex radius; not a measured complete sag.
        ocl_semiaxis_um=min(fact('pixel_pitch_um')/2, (fact('microlens_height_um')*fact('microlens_vertex_radius_um'))**.5),
        dti_oxide_liner_um=.065, ocl_shift_um=0.0)
    materials = copy.deepcopy(model['material_bundle']['materials'])
    materials.update(copy.deepcopy(model['material_bundle']['channels']))
    rows = []
    used = {'pixel_pitch_um','active_si_thickness_um','microlens_height_um',
            'microlens_vertex_radius_um','green_cfa_thickness_um','optical_stack_height_um',
            'active_pixel_dti_depth_um','active_pixel_dti_average_width_um'}
    for name, f in observations.items():
        rows.append(dict(field=name, value=f['value'], unit=f.get('unit'),
            source_id=f.get('source_id'), page=f.get('pdf_page_1_based'),
            status='calculation_input' if name in used else 'retained_unmodeled',
            notes=f.get('notes','')))
    assumptions = {
        'ocl_surface': 'elliptical cap constrained by reported height/radius; pitch-limited footprint',
        'dti_liner': '65 nm oxide per side extended over depth as a sensitivity assumption; local reported dimension only',
        'dti_core': 'undoped poly-Si represented by bulk Si n,k proxy, not measured poly-Si',
        'dti_oxide': 'shared passivation n,k proxy',
        'cfa': 'green cross-section; shared pigment n,k; 3D Bayer R/B thickness 0.475 um assumed',
        'ar_and_pyramids': 'not resolved; fixed lumped passivation retained for controlled DTI/OCL comparison',
        'collection': 'optical absorption only, no carrier transport or product QE',
        'bottom': 'Si continues into lower PML; active absorption ends at reported Si depth',
    }
    result = dict(schema='camera_reviewed_sensor_structure_v1', report_code='DEF-2206-805',
        geometry=geometry, materials=materials, evidence=rows, sources=reviewed['sources'],
        evidence_sha256=evidence_hash, catalog_sha256=hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        corrections=[dict(field='dti_depth_um', original=record['derived_specs'].get('dti_depth_um'),
            reviewed=geometry['dti_depth_um'], width_um=geometry['dti_width_um'],
            source_report='DEP-2206-801', page=67, status='reviewed_revision_original_preserved')],
        assumptions=assumptions, measured_accuracy=False)
    result['revision'] = digest(result)
    return result


def tasks(spec, bundle):
    out = []
    for variant in spec['variants']:
        for wave in spec['wavelength_nm']:
            for angle in spec['angles_deg']:
                for pol in ('s','p'):
                    for res in spec['resolutions']:
                        task = dict(structure=bundle, variant=variant, wavelength_nm=wave,
                            angle_deg=angle, polarization=pol, resolution=int(res),
                            dimension=spec['dimension'], neighborhood=spec['neighborhood'],
                            guard_cells=spec['guard_cells'], pml_um=spec['pml_um'],
                            after_source_time=spec['after_source_time'], source_sigma_pitch=spec['source_sigma_pitch'])
                        task['id'] = digest(task)
                        out.append(task)
    return out


def assess(samples, spec):
    """A completed solver process is distinct from numerical qualification."""
    groups = {}
    for sample in samples:
        key = (sample['variant'],sample['wavelength_nm'],sample['angle_deg'],sample['polarization'])
        groups.setdefault(key,[]).append(sample)
    checks = []
    for key, values in groups.items():
        values.sort(key=lambda s:s['resolution'])
        if any(not np.isfinite(v['pixel_absorption']).all() or not np.isfinite(v['energy_residual']) for v in values):
            checks.append(dict(key=list(key),mesh_relative_change=None,status='unqualified',reasons=['invalid_numerical_result']))
            continue
        if len(values)<2:
            checks.append(dict(key=list(key),mesh_relative_change=None,status='unqualified',reasons=['missing_resolution']))
            continue
        a,b=values[-2:]
        change=float(np.max(np.abs(np.asarray(a['pixel_absorption'])-b['pixel_absorption']))/
                     max(np.max(np.abs(b['pixel_absorption'])), .01))
        reasons=[]
        if sorted(v['resolution'] for v in values)!=spec['resolutions']: reasons.append('missing_resolution')
        if max(abs(v['energy_residual']) for v in values)>.01: reasons.append('energy_balance')
        if any(not v['passive'] for v in values): reasons.append('nonpassive_result')
        if change>(.05 if spec['dimension']==2 else .03): reasons.append('mesh_refinement')
        # Finite-source collected support is conditional on active Si absorption.
        if b.get('support_tail_fraction',0)>.015: reasons.append('support_tail')
        # Time/PML controls are separate samples; never imply they were run.
        reasons.extend(k for k in ('time_refinement','pml_refinement') if not b.get(k+'_passed',False))
        checks.append(dict(key=list(key), mesh_relative_change=change,
                           status='qualified' if not reasons else 'unqualified', reasons=reasons))
    expected=len(spec['variants'])*len(spec['wavelength_nm'])*len(spec['angles_deg'])*2
    expected_keys={(v,w,a,p) for v in spec['variants'] for w in spec['wavelength_nm'] for a in spec['angles_deg'] for p in ('s','p')}
    qualified=set(groups)==expected_keys and all(c['status']=='qualified' for c in checks)
    return dict(status='qualified' if qualified else 'unqualified', checks=checks,
                expected_groups=expected, camera_response_eligible=qualified and spec['dimension']==3,
                scope='2D optical diagnostic; not 3D QE or spatial PSF' if spec['dimension']==2 else '3D optical absorption; scalar collection remains a separate assumption')

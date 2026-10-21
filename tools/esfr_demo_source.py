"""Resolve a declared DB source or create a fresh one using HTTP only."""
from __future__ import annotations

import json
from pathlib import Path
import time


def arguments(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--source-json', type=Path, help='A source.json containing run_id, candidate_id and optional condition_id')
    group.add_argument('--source-run', help='Completed DB Design Lab run in this server')
    group.add_argument('--create-db-source', action='store_true', help='Create a new Reference lens × IMX462 DB run (default when no source is supplied)')
    parser.add_argument('--candidate', help='Candidate for --source-run')
    parser.add_argument('--condition', help='Condition for --source-run, only for conditional DB studies')
    parser.add_argument('--timeout', type=float, default=10800, help='Overall deadline in seconds')


def source_selection(request, args, output, *, check=lambda: None, sleep=time.sleep):
    output = Path(output)
    receipt = output / 'source.json'
    if (args.candidate or args.condition) and not args.source_run:
        raise ValueError('--candidate/--condition require --source-run')
    if args.source_run and not args.candidate:
        raise ValueError('--source-run requires an explicit --candidate')
    declared = None
    if args.source_json:
        declared = json.loads(args.source_json.read_text())
    elif args.source_run:
        declared = {'run_id': args.source_run, 'candidate_id': args.candidate,
                    'condition_id': args.condition}
    if isinstance(declared, dict) and declared.get('condition_id') is None:
        declared.pop('condition_id', None)
    if receipt.exists():
        source = json.loads(receipt.read_text())
        if isinstance(source, dict) and source.get('condition_id') is None:source.pop('condition_id', None)
        if declared is not None and source != declared:
            raise ValueError('Output folder belongs to a different DB source; use a new folder')
    elif declared is not None:
        source = declared
    else:
        submitted = output / 'db-source-submitted.json'
        if submitted.exists():
            run = json.loads(submitted.read_text())
        else:
            run = request('/api/v1/db-design/studies', {
                'name': 'Independent E-SFR synthetic example source',
                'lens_keys': ['reference'], 'sensor_keys': ['reference_imx462'],
                'comparison_mode': 'lens_sensor'})
            submitted.write_text(json.dumps(run, indent=2) + '\n')
        while True:
            check()
            run = request('/api/v1/reference-workshop/runs/' + run['id'])
            if run['status'] == 'succeeded':
                break
            if run['status'] in {'failed', 'cancelled', 'canceled'}:
                raise RuntimeError('DB source calculation failed: ' + str(run.get('error')))
            sleep(3)
        candidates = [c for c in run['result']['summary']['candidates'] if c.get('status') == 'succeeded']
        if len(candidates) != 1:
            raise ValueError('Fresh source must contain exactly one completed candidate')
        source = {'run_id': run['id'], 'candidate_id': candidates[0]['id']}
    if (not isinstance(source, dict) or set(source) - {'run_id', 'candidate_id', 'condition_id'}
            or not isinstance(source.get('run_id'), str) or not isinstance(source.get('candidate_id'), str)):
        raise ValueError('Source JSON requires run_id, candidate_id and optional condition_id')
    run = request('/api/v1/reference-workshop/runs/' + source['run_id'])
    if run.get('plugin_id') != 'camera_db_design_study_v1' or run.get('status') != 'succeeded':
        raise ValueError('Source must be a completed DB Design Lab run in this server')
    selection = run['result']['summary']
    if selection.get('comparison_mode') == 'aperture_conditions':
        conditions = [c for c in selection.get('aperture_conditions', {}).get('conditions', [])
                      if c.get('id') == source.get('condition_id')]
        if len(conditions) != 1:
            raise ValueError('Declare an exact existing condition')
        selection = conditions[0]
    elif source.get('condition_id') is not None:
        raise ValueError('This DB source does not accept a condition')
    if not any(c.get('id') == source['candidate_id'] and c.get('status') == 'succeeded'
               for c in selection.get('candidates', [])):
        raise ValueError('Selected completed candidate does not exist')
    receipt.write_text(json.dumps(source, indent=2) + '\n')
    return source


def bind_server(output, health):
    """Receipts are server-scoped; never silently resume them on a fresh server."""
    path = Path(output) / 'server-binding.json'
    binding = {key: health.get(key) for key in ('workspace', 'state_dir', 'source_code')}
    if not binding['state_dir']:
        raise ValueError('Server must declare its state identity')
    if path.exists() and json.loads(path.read_text()) != binding:
        raise ValueError('Output receipts belong to another server/source; use a new folder')
    path.write_text(json.dumps(binding, indent=2) + '\n')


def latest_query(source):
    from urllib.parse import urlencode
    return urlencode({key: value for key, value in {
        'source_run': source['run_id'], 'candidate': source['candidate_id'],
        'condition': source.get('condition_id')}.items() if value is not None})

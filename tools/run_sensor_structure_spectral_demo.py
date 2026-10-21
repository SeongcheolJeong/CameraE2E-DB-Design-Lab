#!/usr/bin/env python3
"""Submit the real-IMX662, three-wavelength OCL camera projection.

The separately budgeted spectral solver campaign must already be running or
complete. This command never launches Meep or resets its budget.
"""
import argparse
import json
from pathlib import Path

from camera_platform.sdk import PlatformClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8785')
    parser.add_argument('--output', type=Path, default=Path('outputs/ocl-spectral-camera-20261018'))
    parser.add_argument('--submit-only', action='store_true')
    parser.add_argument('--run', help='Resume waiting on an existing Registry run')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with PlatformClient(args.base_url, timeout=120) as client:
        if args.run:
            job = client.sensor_structure_spectral_study(args.run)
        else:
            spec = client.sensor_structure_spectral_template()['specification']
            job = client.submit_sensor_structure_spectral_study(spec)
        (args.output/'receipt.json').write_text(json.dumps(job, indent=2, ensure_ascii=False))
        url = args.base_url+'/sensor-structure-spectral.html?run='+job['id']
        print(job['id'], job['status'], url, flush=True)
        if args.submit_only:
            return
        result = client.wait(job['id'], timeout=32400, poll_interval=5,
                             expected_kind='sensor_structure_spectral_camera')
        (args.output/'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False))
        if result['status'] != 'succeeded':
            raise RuntimeError(str(result.get('error')))
        print(url, flush=True)


if __name__ == '__main__':
    main()

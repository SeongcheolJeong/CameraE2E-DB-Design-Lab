#!/usr/bin/env python3
"""Plot stored Worker RAW inverse evidence; never use simulated charge truth."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from camera_platform import nonlinear_readout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    with zipfile.ZipFile(args.bundle) as archive:
        run = json.loads(archive.read('manifest.json'))['run']
        summary = run['result']['summary']
        retained = {}
        sources = {}
        for name in ('raw_frames.npy', 'calibration_dark_e.npy'):
            artifact = next(a for a in run['artifacts'] if a['name'] == name)
            content = archive.read(f"artifacts/{artifact['sha256']}/{name}")
            if hashlib.sha256(content).hexdigest() != artifact['sha256']:
                raise ValueError('Stored artifact SHA mismatch')
            retained[name] = np.load(io.BytesIO(content), allow_pickle=False)
            sources[name] = artifact['sha256']
    raw, dark = retained['raw_frames.npy'], retained['calibration_dark_e.npy']
    decoded, wrong, exposures, calibrations = [], [], [], []
    for index in range(2):
        cal = {k: v for k, v in summary['calibrations'][index].items()
               if k not in {'frame_index', 'dark_e_array'}}
        cal['dark_e'] = dark[index]
        decoded.append(nonlinear_readout.decode_raw(raw[index], cal)['arrays'])
        settings, model = cal['acquisition'], cal['model']
        if model['digital_gain'] != 1:
            raise ValueError('Illustration assumes digital gain1; it is not a general v1 decoder')
        curve, adc = model['charge_voltage_response'], model['adc']
        first_slope = curve['signal_voltage_v'][1] / curve['charge_e'][1]
        voltage = ((raw[index] - adc['black_level_dn']) * adc['full_scale_v']
                   / (2 ** adc['bit_depth'] - 1) / settings['analog_gain'])
        exposure = settings['exposure_s']
        wrong.append((voltage / first_slope - dark[index]) / exposure)
        exposures.append(exposure)
        calibrations.append(cal)
    knot = calibrations[0]['model']['charge_voltage_response']['charge_e'][1]
    joint = decoded[0]['valid_mask'] & decoded[1]['valid_mask']
    spanning = (joint & (decoded[0]['total_charge_e'] < knot)
                & (decoded[1]['total_charge_e'] > knot))
    rates = [(d['total_charge_e'] - dark[i]) / exposures[i]
             for i, d in enumerate(decoded)]
    usable = spanning & (rates[0] > 0) & (wrong[0] > 0)
    if not usable.any():
        raise ValueError('No jointly usable RAW pixels spanning the internal response knot')
    correct_delta = 100 * (rates[1][usable] / rates[0][usable] - 1)
    wrong_delta = 100 * (wrong[1][usable] / wrong[0][usable] - 1)
    curve = calibrations[0]['model']['charge_voltage_response']
    fig, ax = plt.subplots(2, 2, figsize=(11, 7), constrained_layout=True)
    ax[0, 0].plot(curve['charge_e'], curve['signal_voltage_v'], 'o-',
                  label='Authored nonlinear response')
    ax[0, 0].plot(curve['charge_e'], np.asarray(curve['charge_e']) * first_slope,
                  '--', color='#b86439', label='Origin slope extended (incorrect)')
    ax[0, 0].set(xlabel='Collected charge (electron)', ylabel='Signal voltage (V)',
                  title='Fixed behavioral response; estimated provenance')
    ax[0, 0].legend(fontsize=8)
    for values, label, color in [(wrong_delta, 'Fixed origin-slope inverse', '#b86439'),
                                 (correct_delta, 'Bound nonlinear RAW inverse', '#297967')]:
        ax[0, 1].hist(values, bins=20, alpha=.7, label=label, color=color)
    ax[0, 1].axvline(0, color='black', linewidth=.8)
    ax[0, 1].set(xlabel='Second / first estimated electron rate - 1 (%)',
                  ylabel='Pixels', title='Same scene, different exposures and curve segments')
    ax[0, 1].legend(fontsize=8)
    ratio = np.full(joint.shape, np.nan)
    ratio[usable] = correct_delta
    limit = max(float(np.max(np.abs(correct_delta))), 1.)
    im = ax[1, 0].imshow(ratio, cmap='coolwarm', vmin=-limit, vmax=limit, interpolation='nearest')
    ax[1, 0].set_title('Nonlinear inverse: observed rate disagreement (%)')
    fig.colorbar(im, ax=ax[1, 0], shrink=.8)
    ax[1, 1].axis('off')
    metrics = {
        'joint_valid_pixels': int(joint.sum()), 'spanning_pixels': int(usable.sum()),
        'exposures_s': exposures,
        'nonlinear_median_disagreement_percent': float(np.median(correct_delta)),
        'constant_slope_median_disagreement_percent': float(np.median(wrong_delta)),
        'nonlinear_95th_abs_disagreement_percent': float(np.percentile(np.abs(correct_delta), 95)),
    }
    ax[1, 1].text(0, .98,
        f"Retained Worker RAW + hash-bound calibration only\n"
        f"Exposure: {exposures[0] * 1000:g} / {exposures[1] * 1000:g} ms\n"
        f"Usable pixels spanning the knot: {usable.sum()}\n\n"
        f"Median rate change with nonlinear inverse: {np.median(correct_delta):.3f}%\n"
        f"With incorrect origin-slope inverse: {np.median(wrong_delta):.2f}%\n\n"
        "White: invalid or not spanning the response knot.\n"
        "Noise, quantization and censoring remain.\n"
        "This is an observed pair consistency comparison,\n"
        "not a truth-error or confidence estimate.\n\n"
        "Physical validation: not established.\nNo simulation charge truth enters this analysis.",
        va='top', fontsize=9.5)
    fig.suptitle('CameraE2E | Nonlinear readout and RAW-only inverse | stored evidence',
                 fontsize=13)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=170)
    plt.close(fig)
    report = {'schema': 'camera_nonlinear_raw_plot_v1', 'run_id': run['id'],
              'bundle_sha256': hashlib.sha256(args.bundle.read_bytes()).hexdigest(),
              'artifact_hashes': sources, 'metrics': metrics,
              'basis': 'Retained integer RAW and declared calibration, excluding simulation truth',
              'physical_validation': 'not_established',
              'interpretation': 'Pairwise consistency under authored model, not physical accuracy'}
    args.output.with_suffix('.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(metrics))


if __name__ == '__main__':
    main()

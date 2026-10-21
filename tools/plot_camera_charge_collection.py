#!/usr/bin/env python3
"""Plot retained charge-collection Worker artifacts; verify bytes before loading."""
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    record = json.loads((args.evidence_dir / 'routing-spatial.json').read_text())
    arrays, evidence = {}, {}
    with zipfile.ZipFile(args.evidence_dir / 'routing-spatial-bundle.zip') as archive:
        for name in ('collection_generated', 'mean_electrons', 'collection_intrinsic_loss',
                     'collection_off_sensor_loss', 'raw_frames', 'rgb_frames'):
            descriptor = next(a for a in record['artifacts'] if a['name'] == name + '.npy')
            target = next(n for n in archive.namelist()
                          if n.startswith('artifacts/' + descriptor['sha256'] + '/'))
            content = archive.read(target)
            if (len(content) != descriptor['bytes']
                    or hashlib.sha256(content).hexdigest() != descriptor['sha256']):
                raise ValueError('Retained artifact failed byte verification: ' + name)
            arrays[name] = np.load(io.BytesIO(content), allow_pickle=False)
            evidence[name] = descriptor
    fig, axes = plt.subplots(2, 3, figsize=(13, 7.4), constrained_layout=True)
    fig.suptitle('Generated charge → collection → actual RAW / RGB\n'
                 'Authored 16×16 fixture · physical validation not established', fontsize=15)
    titles = ['Source generation (e−/pixel)', 'Destination collection (e−/pixel)',
              'Collection loss at source (e−/pixel)', 'Off-sensor loss at source (e−/pixel)',
              'Captured integer RAW, frame 0 (DN)', 'ISP output, frame 0']
    for axis, (name, array), title in zip(axes.flat, arrays.items(), titles, strict=True):
        data = array[0] if name in {'raw_frames', 'rgb_frames'} else array
        artist = axis.imshow(data, interpolation='nearest', cmap='viridis')
        axis.set(title=title, xlabel='Native column', ylabel='Native row')
        if data.ndim == 2:
            fig.colorbar(artist, ax=axis, fraction=.046)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=160)
    plt.close(fig)
    proof = {'source_run_id': record['id'], 'source_artifacts': evidence,
             'plot_sha256': hashlib.sha256(args.output.read_bytes()).hexdigest(),
             'scope': 'Retained artifacts only; loss maps use source coordinates; '
                      'collected maps use destination coordinates'}
    args.output.with_suffix('.json').write_text(json.dumps(proof, indent=2) + '\n')
    print(json.dumps({'output': str(args.output), 'source_run_id': record['id']}))


if __name__ == '__main__':
    main()

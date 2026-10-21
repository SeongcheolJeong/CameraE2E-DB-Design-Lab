"""Install and verify the read-only synthetic example pack; never import live state."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def install(archive, workspace):
    workspace = Path(workspace)
    value = json.loads((workspace / 'EXAMPLE_PACK_MANIFEST.json').read_text())
    if value['schema'] != 'camera_esfr_example_pack_v1':
        raise ValueError('Unsupported example pack schema')
    identity = value['id']
    if PurePosixPath(identity).parts != (identity,) or identity in {'.', '..'}:
        raise ValueError('Invalid example identity')
    expected = value['files']
    for name, row in expected.items():
        path = PurePosixPath(name)
        if (path.is_absolute() or '..' in path.parts or '\\' in name or str(path) != name
                or path.parts[0] != identity or type(row['bytes']) is not int or row['bytes'] < 0):
            raise ValueError('Invalid example manifest path or size')
    if sha(archive) != value['archive_sha256']:
        raise ValueError('Example archive SHA256 mismatch')
    base = workspace / 'examples-data'
    if base.is_symlink():
        raise ValueError('Examples directory must not be a symbolic link')
    base.mkdir(exist_ok=True)
    target = base / identity
    if target.exists() or target.is_symlink():
        raise ValueError('Example already exists; originals will not be overwritten')
    with tempfile.TemporaryDirectory(prefix='.install-', dir=base) as temporary:
        with tarfile.open(archive, 'r:gz') as bundle:
            members = bundle.getmembers()
            if (len(members) != len(expected) or {m.name for m in members} != set(expected)
                    or any(not m.isfile() or m.size != expected[m.name]['bytes'] for m in members)):
                raise ValueError('Unexpected, duplicate or unsafe example archive member')
            bundle.extractall(temporary, filter='data')
        for name, row in expected.items():
            if sha(Path(temporary) / name) != row['sha256']:
                raise ValueError('Example file SHA256 mismatch: ' + name)
        if target.exists():
            raise ValueError('Example appeared during install')
        os.rename(Path(temporary) / identity, target)
    return {'status': 'installed', 'example_id': identity, 'files': len(expected),
            'source_kind': 'synthetic', 'live_state_imported': False,
            'url': '/examples/' + identity + '/index.html'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    print(json.dumps(install(args.archive, Path(__file__).resolve().parents[1]), indent=2))

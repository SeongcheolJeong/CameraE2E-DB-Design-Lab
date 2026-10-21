"""Verify and install the private Release DB seed into an empty workspace."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile


def sha(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def install(archive: Path, workspace: Path) -> dict:
    manifest = json.loads((workspace / 'DB_SEED_MANIFEST.json').read_text())
    target = workspace / 'camerae2e_db'
    if target.exists() or target.is_symlink():
        raise ValueError('camerae2e_db already exists; existing DB will not be overwritten')
    if sha(archive) != manifest['archive_sha256']:
        raise ValueError('DB archive SHA256 mismatch')
    expected = {row['path']: row for row in manifest['files']}
    if len(expected) != len(manifest['files']):
        raise ValueError('Duplicate manifest entries')
    for name in expected:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or path.parts[0] != 'camerae2e_db':
            raise ValueError('Invalid manifest path')
    with tempfile.TemporaryDirectory(prefix='.db-seed-', dir=workspace) as temporary:
        staging = Path(temporary)
        with tarfile.open(archive, 'r:gz') as bundle:
            members = bundle.getmembers()
            if (len(members) != len(expected)
                    or {member.name for member in members} != set(expected)
                    or any(not member.isfile() or member.size != expected[member.name]['bytes']
                           for member in members)):
                raise ValueError('Archive membership, file type or size mismatch')
            bundle.extractall(staging, filter='data')
        for name, row in expected.items():
            if sha(staging / name) != row['sha256']:
                raise ValueError(f'DB file SHA256 mismatch: {name}')
        if target.exists():
            raise ValueError('DB appeared during installation; refusing to overwrite')
        os.rename(staging / 'camerae2e_db', target)
    return {'status': 'installed', 'files': len(expected), 'archive_sha256': manifest['archive_sha256']}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    print(json.dumps(install(args.archive.resolve(), workspace), indent=2))


if __name__ == '__main__':
    main()

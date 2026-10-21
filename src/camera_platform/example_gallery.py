"""Hash-bound, read-only historical synthetic examples, separate from live runs."""
from __future__ import annotations

import hashlib
import html
import json
import mimetypes
from pathlib import Path, PurePosixPath

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse

SCHEMA = 'camera_esfr_example_pack_v1'
CSP = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'none'; frame-ancestors 'self'; base-uri 'none'"


def manifest(workspace):
    path = Path(workspace) / 'EXAMPLE_PACK_MANIFEST.json'
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    if value.get('schema') != SCHEMA:
        raise ValueError('Unsupported example pack')
    return value


def catalog(workspace):
    value = manifest(workspace)
    if value is None:
        return {'examples': []}
    identity = value['id']
    return {'examples': [{**value['example'], 'id': identity,
        'installed': (Path(workspace) / 'examples-data' / identity / 'index.html').is_file(),
        'url': '/examples/' + identity + '/index.html',
        'source_kind': 'synthetic', 'physical_validation': 'not_established',
        'live_workflow_registered': False, 'archive_sha256': value['archive_sha256']}]}


def file_response(workspace, identity, relative):
    value = manifest(workspace)
    if value is None or identity != value['id']:
        raise HTTPException(404, 'Example not installed')
    name = PurePosixPath(relative)
    if (not relative or name.is_absolute() or '..' in name.parts or '\\' in relative
            or str(name) != relative):
        raise HTTPException(404, 'Unknown example file')
    key = identity + '/' + relative
    expected = value['files'].get(key)
    if expected is None:
        raise HTTPException(404, 'File is outside the example manifest')
    base = Path(workspace) / 'examples-data'
    if base.is_symlink():
        raise HTTPException(409, 'Examples directory is a symbolic link')
    path = base / key
    # Inspect every component: an installed tree cannot become an external symlink.
    cursor = base
    for part in PurePosixPath(key).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise HTTPException(409, 'Example contains a symbolic link')
    if not path.is_file():
        raise HTTPException(404, 'Install the example pack first')
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if path.stat().st_size != expected['bytes'] or actual != expected['sha256']:
        raise HTTPException(409, 'Example file failed its recorded SHA256')
    media = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    return FileResponse(path, media_type=media, headers={
        'Content-Security-Policy': CSP, 'X-Content-Type-Options': 'nosniff',
        'Cache-Control': 'no-store', 'X-Example-Scope': 'historical-synthetic-read-only'})


def index_response(workspace):
    rows = catalog(workspace)['examples']
    cards = []
    for row in rows:
        title = html.escape(row['title'])
        action = ('<a href="' + html.escape(row['url'], quote=True) + '">예제 열기 →</a>'
                  if row['installed'] else '<p>설치 필요: README의 예제 Release 다운로드 및 install_esfr_examples.py 명령을 실행하세요.</p>')
        cards.append('<article><h2>' + title + '</h2><p>가상 실측 RAW·ISP 12쌍 · 112개 edge · Color·Noise·Tone · 일반/HDR A/B</p><p class="warning">RAW MTF 기준 초과: 일반 2/336 · HDR 5/336. 실패를 보존하며 IQ 합격 판정은 보류합니다.</p>' + action + '</article>')
    body = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>합성 검증 예제 · CameraE2E</title><style>body{font:17px/1.65 system-ui;max-width:1080px;margin:50px auto;padding:0 24px;background:#f5f7f4;color:#203732}article{background:white;border:1px solid #cad6cf;border-radius:16px;padding:28px;margin:24px 0}a{color:#076c62}.warning{color:#923414}nav{display:flex;gap:24px}</style><nav><a href="/db-design.html">DB Design Lab</a><a href="/esfr-measured.html">E-SFR 실측 작업</a></nav><h1>가상 실측 기반 검증 예제</h1><p>과거 합성 실험의 원본과 보정 전후 결과를 읽기 전용으로 봅니다. 새 카메라 측정이나 현재 DB의 검증·활성 모델로 등록하지 않습니다.</p>'''
    return HTMLResponse(body + ''.join(cards) + '</html>', headers={'Content-Security-Policy': CSP})


def install_routes(app, service):
    @app.get('/api/v1/examples')
    def examples():
        return catalog(service.workspace)

    @app.get('/examples/')
    def example_index():
        return index_response(service.workspace)

    @app.get('/examples/{identity}/{relative:path}')
    def example_file(identity: str, relative: str):
        return file_response(service.workspace, identity, relative)

"""Package preserved synthetic results for read-only browsing on a fresh Lab.

Inputs are the original 2026-10-19 evidence directory and its original state
directory. This tool never reads a registry DB, imports run records, or refits.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
from urllib.parse import unquote, urlsplit
import zipfile

IDENTITY = 'esfr-normal-hdr-v1'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def extract_zip(source, destination):
    destination.mkdir(parents=True)
    with zipfile.ZipFile(source) as bundle:
        manifest = json.loads(bundle.read('bundle-manifest.json'))
        expected = {row['path']: row for row in manifest['files']}
        assert len(bundle.namelist()) == len(set(bundle.namelist()))
        assert set(bundle.namelist()) == set(expected) | {'bundle-manifest.json'}
        assert bundle.testzip() is None
        for name in bundle.namelist():
            path = PurePosixPath(name)
            assert not path.is_absolute() and '..' not in path.parts
            data = bundle.read(name)
            if name in expected:
                assert hashlib.sha256(data).hexdigest() == expected[name]['sha256']
                assert len(data) == expected[name]['bytes']
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []

    def handle_starttag(self, tag, attrs):
        self.items.extend(value for key, value in attrs if key in {'src', 'href'} and value)


def portable_report(path, root, routes):
    original = path.read_text()
    changes = []
    def rewrite(match):
        attr, quote, value = match.groups()
        url = html.unescape(value)
        if url in routes:
            target = os.path.relpath(root / routes[url], path.parent)
            changes.append({'original': url, 'portable': target})
            return attr + '=' + quote + html.escape(target, quote=True) + quote
        if url.startswith('/') or url.startswith('http://127.0.0.1:'):
            changes.append({'original': url, 'portable': None})
            return 'data-original-' + attr + '=' + quote + html.escape(url, quote=True) + quote + ' title="과거 실행 링크 · 현재 서버의 작업이 아닙니다"'
        return match.group(0)
    text = re.sub(r'\b(href|src)=(\"|\x27)(.*?)\2', rewrite, original)
    notice = '<aside style="padding:16px;background:#fff1d9;color:#493d28;border:2px solid #a77532">보존된 합성 실험 · 읽기 전용 예제입니다. 새 검증/실측/활성 모델이 아닙니다. 일반 RAW 2/336, HDR RAW 5/336의 기준 초과를 보존합니다. <a href="' + os.path.relpath(root / 'index.html', path.parent) + '">예제 목록·원본 데이터</a></aside>'
    text = re.sub(r'(<body[^>]*>)', lambda m: m.group(1) + notice, text, count=1)
    path.write_text(text)
    return {'path': str(path.relative_to(root)), 'original_sha256': hashlib.sha256(original.encode()).hexdigest(),
            'portable_sha256': sha(path), 'changes': changes,
            'scope': 'Links and historical-view notice only; numerical tables and image bytes unchanged'}


def build(evidence, state, output, manifest_path):
    output.mkdir(parents=True, exist_ok=True)
    root = output / IDENTITY
    root.mkdir(exist_ok=False)
    inputs = []
    mode_reports = {}
    routes = {}
    originals = {}
    report_routes = {}
    for mode in ('normal', 'hdr'):
        original_zip = evidence / 'live' / mode / 'evidence.zip'
        extract_zip(original_zip, root / mode)
        shutil.copy2(original_zip, root / mode / 'bundle.zip')
        originals[mode] = sha(original_zip)
        report = json.loads((root / mode / 'iq-result.json').read_text())
        assert report['source_kind'] == 'synthetic'
        mode_reports[mode] = report
        for role in ('development', 'validation'):
            for i in range(3):
                source = evidence / 'live' / mode / f'{role}-{i}'
                target = root / 'inputs' / mode / f'{role}-{i}'
                target.mkdir(parents=True)
                metadata = json.loads((source / 'metadata.json').read_text())
                assert metadata['source_kind'] == 'synthetic' and metadata['role'] == role
                for filename in ('capture.raw', 'capture.tiff', 'metadata.json'):
                    shutil.copy2(source / filename, target / filename)
                    assert sha(source / filename) == sha(target / filename)
                inputs.append({'mode': mode, 'role': role, 'index': i,
                    'raw': str((target / 'capture.raw').relative_to(root)),
                    'isp': str((target / 'capture.tiff').relative_to(root)),
                    'metadata': str((target / 'metadata.json').relative_to(root)),
                    'raw_sha256': sha(target / 'capture.raw'), 'isp_sha256': sha(target / 'capture.tiff'),
                    'session_id': metadata['session_id'], 'acquisition_id': metadata['acquisition_id']})
        workflow = report['workflow_id']
        prefix = '/api/v1/camera-validation/esfr/measured/workflows/' + workflow + '/iq/files/'
        routes[prefix + 'report.html'] = mode + '/report.html'
        routes[prefix + 'combined-replay/result.json'] = mode + '/combined-replay/result.json'
        native_url = report['mtf_evidence']['report_url']
        # Exact native SFR report and all 112-edge curve/image artifacts.
        native_relative = native_url.split('/studies/', 1)[1]
        job_id, suffix = native_relative.split('/files/', 1)
        native_source = state / 'calibration-lab' / job_id / Path(suffix).parent
        target = root / (mode + '-mtf')
        shutil.copytree(native_source, target)
        routes[native_url] = mode + '-mtf/report.html'
        (target / 'source').mkdir()
        shutil.copy2(native_source.parent / 'request.json', target / 'source/request.json')
        report_routes[mode + '-mtf'] = {'../request.json': mode + '-mtf/source/request.json'}
    assert len({x['raw_sha256'] for x in inputs}) == len(inputs) == 12
    assert len({x['isp_sha256'] for x in inputs}) == 12
    assert len({x['acquisition_id'] for x in inputs}) == 12
    original_zip = evidence / 'current-server-report/verification/evidence.zip'
    extract_zip(original_zip, root / 'comparison')
    shutil.copy2(original_zip, root / 'comparison/bundle.zip')
    originals['comparison'] = sha(original_zip)
    # Original report links report.json while the bundle names it iq-result.json.
    # Preserve the JSON bytes, and explicitly record this portable alias.
    shutil.copy2(root / 'comparison/iq-result.json', root / 'comparison/report.json')
    with (root / 'comparison/ab-mtf-acceptance.csv').open() as f:
        counts = Counter(row['status'] for row in csv.DictReader(f))
    assert counts == {'pass': 1001, 'fail': 7, 'diagnostic': 336}, counts
    index = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>E-SFR 가상 실측 예제</title><style>body{font:17px/1.65 system-ui;max-width:1140px;margin:40px auto;padding:0 22px;background:#f3f6f3;color:#203731}section{background:white;border:1px solid #c7d6cd;border-radius:14px;padding:24px;margin:22px 0}a{color:#006d61}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #dae3dd;padding:10px;text-align:left}.warning{background:#fff0db;color:#743813;padding:18px}img{width:48%;vertical-align:top}nav{display:flex;gap:20px}</style><nav><a href="/examples/">예제 목록</a><a href="/db-design.html">현재 DB Design Lab</a><a href="/esfr-measured.html">새 E-SFR 작업</a></nav><h1>E-SFR 가상 실측 · 일반/HDR 독립 비교</h1><p>2560×1440 · 각 모드 개발 3쌍 + 고정 후 독립 검증 3쌍 · 112개 edge · 18색·20계조</p><p class="warning"><strong>보존된 합성 실험입니다.</strong> 실측 카메라 성능이나 현재 DB의 검증 결과가 아닙니다. 일반 RAW 2/336 · HDR RAW 5/336은 기준 초과이며 HDR ISP 336건은 진단입니다. Color·Noise·Tone 합격 판정은 보류합니다.</p><section><h2>보정 전후와 모드 비교</h2><p><a href="comparison/report.html">색·노이즈·Tone·112개 edge 통합 A/B 보고서 →</a></p><p><a href="normal/report.html">일반 모드 보정 전후</a> · <a href="hdr/report.html">HDR 모드 보정 전후</a></p><p><a href="normal-mtf/report.html">일반 112개 edge 원본·재시뮬레이션 SFR</a> · <a href="hdr-mtf/report.html">HDR 112개 edge 원본·재시뮬레이션 SFR</a></p><img src="comparison/images/mode-a-capture-01-actual_isp.png" alt="일반 합성 ISP"><img src="comparison/images/mode-b-capture-01-actual_isp.png" alt="HDR 합성 ISP"></section><section><h2>원본 근거와 오차</h2><p><a href="normal/bundle.zip">일반 원본 ZIP</a> · <a href="hdr/bundle.zip">HDR 원본 ZIP</a> · <a href="comparison/bundle.zip">A/B 원본 ZIP</a></p><p><a href="comparison/ab-color.csv">색차 CSV</a> · <a href="comparison/ab-noise.csv">노이즈 CSV</a> · <a href="comparison/ab-tone.csv">Tone CSV</a> · <a href="comparison/ab-mtf.csv">MTF CSV</a> · <a href="comparison/ab-mtf-acceptance.csv">MTF 판정 CSV</a></p><p><a href="normal/frozen-iq.json">일반 고정 IQ 프로파일</a> · <a href="hdr/frozen-iq.json">HDR 고정 IQ 프로파일</a> · <a href="provenance.json">원본 SHA·변경 내역</a></p></section><section><h2>12개 가상 실측 RAW·ISP 원본</h2><p>아래 validation 파일은 과거 검증에 이미 사용된 보존 데이터입니다. 새 독립 검증으로 재등록하지 않습니다. 새 계산은 run_esfr_hdr_ab_live.py가 새 seed와 별도 세션에서 취득합니다.</p><table><tr><th>모드</th><th>역할</th><th>번호</th><th>RAW</th><th>ISP</th><th>설정</th></tr>'''
    for row in inputs:
        index += '<tr><td>' + row['mode'] + '</td><td>' + row['role'] + '</td><td>' + str(row['index'] + 1) + '</td>' + ''.join('<td><a href="' + row[k] + '">' + label + '</a></td>' for k, label in [('raw', 'RAW'), ('isp', 'TIFF'), ('metadata', 'JSON')]) + '</tr>'
    (root / 'index.html').write_text(index + '</table></section></html>')
    rewrites = [portable_report(root / folder / 'report.html', root, {**routes, **report_routes.get(folder, {})})
                for folder in ('normal', 'hdr', 'comparison', 'normal-mtf', 'hdr-mtf')]
    provenance = {'schema': 'esfr_historical_example_provenance_v1', 'source_kind': 'synthetic',
        'physical_validation': 'not_established', 'live_workflow_registered': False,
        'original_zip_sha256': originals, 'inputs': inputs, 'mtf_check_counts': dict(counts),
        'numerical_refit_performed': False, 'report_portability_changes': rewrites,
        'report_json_alias': 'comparison/report.json is byte-identical to comparison/iq-result.json'}
    (root / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    checked = 0
    for p in root.rglob('*.html'):
        parser = Links(); parser.feed(p.read_text())
        for link in parser.items:
            if link.startswith(('/', '#', 'data:', 'http:', 'https:')):
                continue
            target = p.parent / unquote(urlsplit(link).path)
            assert target.is_file(), (p, link)
            assert target.resolve().is_relative_to(root.resolve()), (p, link)
            checked += 1
    files = {}
    archive = output / 'esfr-normal-hdr-examples-v1.tar.gz'
    with tarfile.open(archive, 'w:gz', compresslevel=3) as bundle:
        for p in sorted(root.rglob('*')):
            if p.is_file():
                name = str(p.relative_to(output))
                files[name] = {'sha256': sha(p), 'bytes': p.stat().st_size}
                bundle.add(p, arcname=name, recursive=False)
    value = {'schema': 'camera_esfr_example_pack_v1', 'id': IDENTITY,
        'repository': 'SeongcheolJeong/CameraE2E-DB-Design-Lab',
        'release_tag': 'esfr-examples-v1', 'asset_name': archive.name,
        'archive_sha256': sha(archive), 'archive_bytes': archive.stat().st_size,
        'example': {'title': 'E-SFR 일반/HDR · 가상 실측 12쌍', 'input_pairs': 12,
                    'edge_count': 112, 'raw_mtf_failures': {'normal': 2, 'hdr': 5},
                    'iq_acceptance': 'pending', 'historical_evidence': True},
        'portable_links_verified': checked, 'files': files}
    manifest_path.write_text(json.dumps(value, indent=2) + '\n')
    print(json.dumps({'archive': str(archive), 'archive_bytes': archive.stat().st_size,
                     'files': len(files), 'links_checked': checked, 'sha256': value['archive_sha256']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-root', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    build(args.evidence_root, args.state_dir, args.output, args.manifest)

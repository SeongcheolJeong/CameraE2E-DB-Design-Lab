"""Selected DB model vs independent measured native edges, with fixed nuisance.

This is a discrete, native pixel-integrated four-site PSF replay. It does not
reconstruct a continuous PSF, add a second pixel aperture, fit holdout geometry,
or claim an absolute radiometric/vendor-ISP prediction.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import html
import inspect
import json
import mimetypes
from pathlib import Path
import tempfile
from urllib.parse import urlencode
import zipfile

import numpy as np
from scipy.signal import fftconvolve

from .registry import IntegrityError, canonical, digest
from . import esfr_camera_model as models

KIND = "camera_esfr_model_comparisons"
ROOT = models.ROOT + "/comparisons"

# Explicit report-only transition from the initial adapter. Every other Python
# source/dependency must be byte-identical; fitting/observation never resumes.
_REPORT_PREDECESSOR = 'a223f9bfaffd7843ee74f4d94ad1d3e7ce956a888e8834eaacc4d88496f0c354'
_SIMULATE_SHA256 = 'dae670d16dc14e8941d02e63b68ed9c8e04d31f9d506bb72649cfba83c0e1611'


def report_compatibility(service, record):
    from . import calibration_lab as lab
    current, old = lab._source_code(), record['source_code']
    if service.loaded_environment != current:
        raise IntegrityError('Restart current reporting code before finalization')
    changed = sorted(k for k in set(old['source_hashes']) | set(current['source_hashes']) if old['source_hashes'].get(k)!=current['source_hashes'].get(k))
    permitted = {'camera_platform/esfr_model_prediction.py', 'camera_platform/esfr_measured_workflow.py'}
    if old == current:
        return {'schema':'camera_esfr_report_only_compatibility_v1','permitted_changed_modules':[],
            'all_other_sources_and_dependencies_identical':True,
            'sampled_psf_operator_sha256':hashlib.sha256(inspect.getsource(simulate).encode()).hexdigest(),
            'validation_refit_performed':False}
    if ({k:v for k,v in current.items() if k!='source_hashes'} != {k:v for k,v in old.items() if k!='source_hashes'}
            or not set(changed)<=permitted
            or old['source_hashes'].get('camera_platform/esfr_model_prediction.py') != _REPORT_PREDECESSOR
            or hashlib.sha256(inspect.getsource(simulate).encode()).hexdigest() != _SIMULATE_SHA256):
        raise IntegrityError('Report-only resume cannot change any numerical observer, fitter, dependency or sampled PSF operator')
    return {'schema':'camera_esfr_report_only_compatibility_v1','permitted_changed_modules':changed,
        'all_other_sources_and_dependencies_identical':True,'sampled_psf_operator_sha256':_SIMULATE_SHA256,
        'previous_prediction_source_sha256':_REPORT_PREDECESSOR,'validation_refit_performed':False}


def simulate(kernels, geometry, levels, shape, cfa="RGGB", adc=None):
    from .esfr_engine import bilinear_isp, _labels
    kernels = np.asarray(kernels, float)
    if kernels.ndim != 3 or kernels.shape[0] != 4 or kernels.shape[1] != kernels.shape[2] or kernels.shape[1] % 2 != 1 or kernels.shape[1] > 257:
        raise ValueError("Bounded odd native four-site PSFs required")
    if not np.isfinite(kernels).all() or np.any(kernels < 0) or not np.allclose(kernels.sum((1, 2)), 1, atol=1e-10):
        raise ValueError("Native PSFs require finite nonnegative unit energy")
    h, w = map(int, shape)
    if min(h, w) < 8 or h * w > 1_000_000:
        raise ValueError("Prediction ROI exceeds native budget")
    n = np.asarray(geometry["normal_xy"], float)
    if n.shape != (2,) or not np.isfinite(n).all() or not np.isclose(np.linalg.norm(n), 1):
        raise ValueError("Frozen native edge normal required")
    halo = kernels.shape[-1] // 2 + 2
    yy, xx = np.indices((h + 2*halo, w + 2*halo), dtype=float)
    xx -= halo; yy -= halo
    step = (n[0]*xx + n[1]*yy >= float(geometry["offset_px"])).astype(float)
    origin = geometry["crop_origin"]
    site = ((yy.astype(int) + origin[0]) % 2)*2 + (xx.astype(int) + origin[1]) % 2
    raw = np.zeros(step.shape, np.uint16)
    for i, ch in enumerate(_labels(cfa)):
        v = levels[ch]
        # Each source kernel already includes receiver integration once.
        edge = fftconvolve(step, kernels[i], mode="same")
        value = v["black_dn"] + v["low_dn"] + edge*(v["high_dn"]-v["low_dn"])
        maximum = min(v["white_dn"], 2**adc["bit_depth"]-1) if adc else v["white_dn"]
        raw[site == i] = np.rint(np.clip(value[site == i], 0, maximum)).astype(np.uint16)
    rgb = bilinear_isp(raw, cfa, (origin[0]-halo, origin[1]-halo), levels)
    raw, rgb = raw[halo:-halo, halo:-halo].copy(), rgb[halo:-halo, halo:-halo].copy()
    planes = {}
    for i, ch in enumerate(_labels(cfa)):
        r, c = divmod(i, 2)
        planes[ch] = raw[(r-origin[0]) % 2::2, (c-origin[1]) % 2::2].astype(float)-levels[ch]["black_dn"]
    return {"raw": raw, "isp": rgb, "raw_planes": planes}


def _stats(rows, key):
    values = [r[key] for r in rows if r[key] is not None]
    return {"mean": float(np.mean(values)) if values else None, "max": max(values, default=None),
            "available": len(values), "unavailable": len(rows)-len(values)}


def _display_points(metric):
    f, v = metric.get('frequency_cy_native_px', []), metric.get('sfr', [])
    if len(f) != len(v):
        return []
    indices = np.unique(np.linspace(0, len(f)-1, min(97,len(f))).astype(int)) if f else []
    return [(f[i],v[i]) for i in indices]


def _render(result):
    from .esfr_iq_report import CSS, _plot, _num
    esc = lambda v: html.escape(str(v), quote=True)
    parts = ['<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>선택 카메라 모델 · 실측 비교</title><style>'+CSS+'</style><main><p class="eyebrow">SELECTED DB MODEL → FROZEN CAMERA RESPONSE → INDEPENDENT OBSERVATIONS</p><h1>선택 카메라 모델과 실측 비교</h1>',
        '<p class="notice">'+esc('합성 소프트웨어 통합 검증 · 실제 촬영 아님' if result['source_kind']=='synthetic' else '사용자 실측 입력 · 장비 대응과 촬영 조건은 사용자 선언')+'</p>',
        '<section class="card"><h2>'+esc(result['camera']['manufacturer']+' / '+result['camera']['model'])+'</h2><p>'+esc(result['camera'])+'</p><small>모델 '+esc(result['model_id'])+' · SHA256 '+esc(result['model_sha256'])+'</small><p>RAW: DB 4-site 샘플 PSF → native Bayer·ADC. ISP: 고정 bilinear Reference ISP. 보정 후는 개발 데이터에서 추정하고 고정한 camera-effective 응답입니다.</p><p>선택 DB 광학: '+esc(result['scope']['source_optical_profile'])+' · '+('회절 포함' if result['scope']['diffraction_included'] else '회절 미포함')+'</p><p class="notice">시야 배치: '+esc(result['spatial'])+' · DB 가상 탐침을 실제 센서 위치로 자동 환산하지 않았습니다. 전영역 동일 PSF 선택은 명시적인 공간 불변 가정입니다.</p><p>엣지 위치·밝기·black/white는 개발 영상에서 한 번 고정합니다. 검증 영상으로 다시 맞추지 않습니다. 절대 조도·색·Tone·센서 노이즈의 DB 예측은 추가 입력이 필요합니다.</p></section>',
        '<nav class="card"><a href="'+esc(result['db_url'])+'">선택 DB 모델 ↗</a> · <a href="'+esc(result['workflow_url'])+'">보정 작업 ↗</a> · <a href="metrics.csv">CSV</a> · <a href="bundle.zip">ZIP</a></nav>']
    if result.get('iq_report_url'):
        parts += ['<section class="card"><h2>색·노이즈·Tone·CCM</h2><p>동일 RAW의 고정 Reference ISP/CCM 평가이며 선택 DB의 절대 색·노이즈·Tone 예측으로 표시하지 않습니다.</p><a href="'+esc(result['iq_report_url'])+'">18 Color · 20 Gray · 노이즈 · CCM 통합 근거 ↗</a><pre>'+esc(json.dumps(result.get('iq_summary'), ensure_ascii=False, indent=2))+'</pre></section>']
    parts += ['<h2>독립 검증 112 edges × 3회</h2><p>허용 기준 유지: RAW RMSE 0.03 · ISP RMSE 0.05. 실제 ISP 파이프라인 대응이 미확인인 경우 진단으로 남습니다. 보정 전·후의 RAW는 광학 응답이 달라지므로 같을 필요가 없으며 CCM 단계에서만 동일 RAW를 유지합니다.</p><table><thead><tr><th>평균 곡선 오차</th><th>선택 DB 모델</th><th>고정 보정 후</th><th>기준 초과 / 진단</th></tr></thead><tbody>'+''.join('<tr><td>'+d.upper()+'</td><td>'+_num(result['summary'][d]['db']['mean'])+'</td><td>'+_num(result['summary'][d]['after']['mean'])+'</td><td>'+str(result['summary'][d+'_failure_count'])+' / '+str(result['summary'].get(d+'_diagnostic_count',0))+'</td></tr>' for d in ('raw','isp'))+'</tbody></table><details><summary>가용·누락·최대 오차</summary><pre>'+esc(json.dumps(result['summary'], ensure_ascii=False, indent=2))+'</pre></details>',
        '<div class="scroll"><table><thead><tr><th>ROI / 촬영</th><th>RAW DB 전</th><th>RAW 보정 후</th><th>RAW 판정</th><th>ISP DB 전</th><th>ISP 보정 후</th><th>ISP 판정</th></tr></thead><tbody>']
    for r in result['comparisons']:
        parts.append('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in [r['roi_id']+' / '+r['capture_id'][-8:], _num(r['raw_db_rmse']), _num(r['raw_after_rmse']), r['raw_status'], _num(r['isp_db_rmse']), _num(r['isp_after_rmse']), r['isp_status']])+'</tr>')
    parts.append('</tbody></table></div><h2>위치별 native RAW·ISP·MTF</h2>')
    for r in result['edges']:
        parts.append('<details class="card"><summary>'+esc(r['roi_id'])+' · source PSF '+esc(r['source_field_id'])+'</summary><div class="images">')
        for stage, label in [('measured','실측 / 가상 실측'),('db','선택 DB 모델'),('after','고정 보정 후보')]:
            for kind in ('raw', 'isp'):
                link = r['images'][stage].get(kind)
                if link:
                    parts.append('<figure><img loading="lazy" src="'+esc(link)+'" alt="'+esc(label+' '+kind)+'"><figcaption>'+esc(label+' '+kind.upper())+'</figcaption></figure>')
        parts.append('</div>')
        for domain in ('raw','isp'):
            parts.append(_plot([(label, _display_points(r['curves'][domain][stage])) for stage,label in [('measured','관측'),('db','DB'),('after','고정 보정 후')]], 'cycles/native pixel', domain.upper()+' MTF'))
        parts.append('</details>')
    parts += ['<p>그래프는 최대 97개 표시점을 사용하며 전체 곡선과 계산값은 JSON에 보존합니다.</p><details class="card"><summary>고정 계약·원본·모델 출처</summary><pre>'+esc(json.dumps({k:result[k] for k in ('input_contract','source_context','scope','source_bindings','capture_evidence','source_code','numerical_source_code','report_only_compatibility')},ensure_ascii=False,indent=2))+'</pre></details></main></html>']
    return ''.join(parts)


def create(service, record, state, *, report_only=False):
    from . import calibration_lab as lab, esfr_lab
    from .esfr_engine import _curve_rmse, _save_images
    from .esfr_analysis import edge_metrics
    from .esfr_iq_engine import apply_profile_rgb
    compatibility = report_compatibility(service, record) if report_only else None
    if service.loaded_environment != lab._source_code() or (not report_only and record['source_code'] != service.loaded_environment):
        raise IntegrityError('Selected camera prediction executable changed after workflow freeze')
    captures = [esfr_lab.capture(service, i) for i in record['development_ids']+state['validation_ids']]
    model = models.check_captures(service, record['camera_model_id'], captures, record['source'])
    kernels = models.kernels(service, model)
    validation = lab.get(service, state['validation']['id'])
    if validation['status'] != 'succeeded':
        raise IntegrityError('Selected-model comparison requires completed independent validation')
    if (validation.get('parent_study_id') != state['fit']['id']
            or validation.get('candidate_sha256') != state['fit']['candidate_sha256']
            or validation['result']['summary']['esfr_image_validation'].get('refit_performed') is not False):
        raise IntegrityError('Selected-model comparison changed the completed frozen candidate')
    files = validation['result']['summary']['files']
    bindings_by_sha = {}
    def path(relative):
        checksum = files[relative]
        if checksum not in bindings_by_sha:
            bindings_by_sha[checksum] = service.registry.artifact_binding(checksum, owner_kind='runs', owner_id=validation['id'])
        return bindings_by_sha[checksum]['path']
    with gzip.open(path('esfr-images/full-result.json.gz'), 'rt') as stream:
        native = json.load(stream)
    candidates = native['candidate_profile']
    templates = candidates['geometry']['roi_nuisance']
    comparisons = [r for r in native['comparisons'] if r['role']=='validation']
    if (len(comparisons) != 336 or len({r['roi_id'] for r in comparisons}) != 112
            or {r['capture_id'] for r in comparisons} != set(state['validation_ids'])
            or candidates.get('calibration_profile_sha256') != state['fit']['candidate_sha256']):
        raise IntegrityError('Selected model comparison requires all 112 edges in 3 new captures')
    donors = {r['roi_id']: r for r in comparisons if all(s in r.get('images', {}) for s in ('measured','after'))}
    if len(donors) != 112:
        raise IntegrityError('Selected model comparison requires native measured/after arrays for all edges')
    needed = ['esfr-images/'+r['images'][stage][kind] for r in donors.values()
        for stage, kinds in [('after',('raw_array','isp_array')),('measured',('raw','isp'))] for kind in kinds]
    # Verify the exact typed owner graph ONCE for the full immutable batch.
    # Per-file byte SHA/size/head checks remain mandatory in artifact_bindings.
    bindings_by_sha.update(service.registry.artifact_bindings([files[n] for n in needed], owner_kind='runs', owner_id=validation['id']))
    iq_profile = state.get('iq_frozen', {}).get('profile')
    if record.get('iq', {}).get('enabled'):
        from . import esfr_iq_service as iq_service
        frozen, _ = iq_service._contract(service, record, state)
        iq_profile = frozen['profile']
        if iq_service._report_for_workflow(service, record['id']) != state['iq_result']:
            raise IntegrityError('Finalization IQ receipt differs from its immutable owner')
    key = {'model_sha256':model['content_hash'], 'validation_result_sha256':digest(validation['result']), 'iq_profile_sha256':digest(iq_profile), 'source_code':lab._source_code()}
    identity = 'esfr-model-comparison-'+digest(key)[:32]
    try:
        return service.registry.get(KIND, identity)
    except KeyError:
        pass
    with tempfile.TemporaryDirectory(prefix='esfr-db-model-') as tmp:
        out = Path(tmp)
        cache, edges, bindings = {}, [], []
        for roi_id, donor in sorted(donors.items()):
            template = templates[roi_id]
            geometry, levels = template['geometry'], template['levels']
            field = model['spatial'].get('source_field_id') if model['spatial']['mode']=='uniform_selected_db_field' else model['spatial']['roi_fields'][roi_id]
            shape = geometry['box'][2:]
            rendered = simulate(kernels[field], geometry, levels, shape, model['input_contract']['raw_format']['cfa'], model['electronics']['electronics'])
            before_isp = apply_profile_rgb(iq_profile, rendered['isp'], baseline=True) if iq_profile else rendered['isp']
            after_path = 'esfr-images/'+donor['images']['after']['isp_array']
            raw_path = 'esfr-images/'+donor['images']['after']['raw_array']
            after_raw, after_isp = np.load(path(raw_path), allow_pickle=False), np.load(path(after_path), allow_pickle=False)
            after_isp = apply_profile_rgb(iq_profile, after_isp) if iq_profile else after_isp
            curves = {'raw':{'db':edge_metrics(rendered['raw_planes']['Gr'], pitch_native=2)},
                      'isp':{'db':edge_metrics(before_isp @ np.array([.2126,.7152,.0722])), 'after':edge_metrics(after_isp @ np.array([.2126,.7152,.0722]))}}
            curves['raw']['after'] = donor['curves']['raw']['after']
            for domain in ('raw','isp'):
                curves[domain]['measured'] = donor['curves'][domain]['measured']
            images = {'db':_save_images(out, 'prediction', roi_id, 'db', rendered['raw'], before_isp, levels),
                      'after':_save_images(out, 'prediction', roi_id, 'after', after_raw, after_isp, levels)}
            images['measured'] = {}
            for kind in ('raw','isp'):
                source = 'esfr-images/'+donor['images']['measured'][kind]
                target = out/'observed'/roi_id/(kind+'.png'); target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(path(source).read_bytes())
                images['measured'][kind] = target.relative_to(out).as_posix()
            bindings.append({'roi_id':roi_id, 'source_field_id':field,
                'db_kernel_sha256':model['files']['model/broadband-kernels.npz']['sha256'],
                'nuisance_sha256':digest(template), 'after_raw_sha256':files[raw_path],
                'after_raw_after_ccm_sha256':files[raw_path], 'after_isp_sha256':files[after_path]})
            cache[roi_id] = curves
            edges.append({'roi_id':roi_id,'source_field_id':field,'curves':curves,'images':images})
        rows = []
        for c in comparisons:
            curve = cache[c['roi_id']]
            row = {'capture_id':c['capture_id'], 'roi_id':c['roi_id']}
            for domain, threshold in [('raw',.03),('isp',.05)]:
                measured = c['curves'][domain]['measured']
                for stage in ('db','after'):
                    row[domain+'_'+stage+'_rmse'] = _curve_rmse(measured, curve[domain][stage])
                value = row[domain+'_after_rmse']
                eligible = domain=='raw' or c['isp'].get('acceptance_eligible',True)
                row[domain+'_status'] = 'unavailable' if value is None else 'diagnostic' if not eligible else 'pass' if value<=threshold else 'fail'
            rows.append(row)
        iq = state.get('iq_result') or {}
        result = {'schema':'camera_esfr_selected_model_comparison_v1','workflow_id':record['id'],
            'model_id':model['id'],'model_sha256':model['content_hash'], 'camera':model['camera'],
            'source_kind':record['source_kind'], 'source':record['source'], 'source_context':model['source_context'],
            'input_contract':model['input_contract'],'spatial':model['spatial'],'scope':model['scope'],
            'comparisons':rows,'edges':edges,'source_bindings':bindings,'source_code':key['source_code'],
            'numerical_source_code':record['source_code'],'report_only_compatibility':compatibility,
            'validation_run_id':validation['id'],'validation_result_sha256':digest(validation['result']),
            'capture_evidence':[{'id':c['id'],'role':c['metadata']['role'],
                'raw_sha256':c['files']['raw']['sha256'],'isp_sha256':c['files']['isp']['sha256'],
                'native_pixel_sha256':c['native_pixel_sha256'],'metadata_sha256':digest(c['metadata'])} for c in captures],
            'summary':{**{d:{s:_stats(rows,d+'_'+s+'_rmse') for s in ('db','after')} for d in ('raw','isp')},
                'raw_failure_count':sum(r['raw_status']=='fail' for r in rows), 'raw_unavailable_count':sum(r['raw_status']=='unavailable' for r in rows),
                'isp_failure_count':sum(r['isp_status']=='fail' for r in rows), 'isp_diagnostic_count':sum(r['isp_status']=='diagnostic' for r in rows)},
            'thresholds':record['thresholds'],'validation_refit_performed':False,
            'input_contract_sha256':digest(model['input_contract']), 'mtf_candidate_sha256':state['fit']['candidate_sha256'],
            'iq_profile_sha256':key['iq_profile_sha256'],'iq_report_url':iq.get('report_url'),
            'iq_summary':{k:iq.get('validation',{}).get(k,{}).get('summary') for k in ('color','tone')},
            'db_url':'/db-design.html?'+urlencode({'run':record['source']['run_id'],'candidate-b':record['source']['candidate_id'],**({'condition':record['source']['condition_id']} if record['source'].get('condition_id') else {})}),
            'workflow_url':'/esfr-measured.html?'+urlencode({'workflow':record['id']}),
            'evidence_kind':'software_integration' if record['source_kind']=='synthetic' else 'operator_declared_measured_comparison',
            'physical_validation':'not_established','db_active_model_updated':False,
            'native_pixel_aperture_application_count':1, 'no_gaussian_substitution_for_db':True,
            'deterministic_expected_signal':True,'noise_realization_added':False,
            'limits':['Frozen development plateaus condition the MTF replay; not absolute radiometry.',
                'Sensor electrical model retained; bit depth used in replay, electron gain/read noise retained as provenance, not fitted or applied as extra noise.',
                'Spatial field assignment is explicit and not physically verified.',
                'Post-calibration PSF replaces the effective response; no lens prescription parameters were refitted.']}
        (out/'camera-model.json').write_text(canonical(model))
        (out/'report.json').write_text(canonical(result))
        (out/'report.html').write_text(_render(result))
        with (out/'metrics.csv').open('w',newline='') as stream:
            writer = csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        manifest = {'files':[{'path':p.relative_to(out).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size} for p in sorted(out.rglob('*')) if p.is_file()]}
        (out/'bundle-manifest.json').write_text(canonical(manifest))
        with zipfile.ZipFile(out/'bundle.zip','w',compression=zipfile.ZIP_DEFLATED) as z:
            for p in sorted(out.rglob('*')):
                if p.is_file() and p.name!='bundle.zip':z.write(p,p.relative_to(out).as_posix())
        files, artifacts = {}, []
        for p in sorted(out.rglob('*')):
            if p.is_file():
                a = service.registry.store_file(p,p.relative_to(out).as_posix(),mimetypes.guess_type(p.name)[0] or 'application/octet-stream')
                files[p.relative_to(out).as_posix()] = a['sha256'];artifacts.append(a)
        if service.loaded_environment != lab._source_code():
            raise IntegrityError('Selected camera prediction executable changed during replay')
        document = {k:v for k,v in result.items() if k not in {'edges','comparisons'}}
        document.update(files=files,artifacts=lab._unique_artifacts(artifacts),report_url=ROOT+'/'+identity+'/files/report.html',bundle_url=ROOT+'/'+identity+'/files/bundle.zip')
        from . import esfr_measured_workflow as flow
        return service.registry.put_verified_record(KIND,document,[(models.KIND,model),(flow.KIND,record)],identity=identity)


def install_routes(app, service):
    @app.get(ROOT + "/latest")
    def latest(source_run: str, candidate: str, condition: str | None = None):
        source = {'run_id':source_run,'candidate_id':candidate}
        if condition is not None:source['condition_id']=condition
        rows = [r for r in service.registry.list(KIND) if r['source']==source]
        return {'comparison':max(rows,key=lambda r:(r['created_at'],r['id'])) if rows else None}

    @app.get(ROOT + "/{identity}/files/{relative:path}")
    def file(identity: str, relative: str):
        from fastapi.responses import FileResponse
        row = service.registry.get(KIND,identity)
        if relative not in row['files']:
            raise KeyError('Comparison file not in immutable manifest')
        binding = service.registry.artifact_binding(row['files'][relative],owner_kind=KIND,owner_id=identity)
        return FileResponse(binding['path'],media_type=mimetypes.guess_type(relative)[0] or 'application/octet-stream',
            headers={'Content-Security-Policy':"default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"})

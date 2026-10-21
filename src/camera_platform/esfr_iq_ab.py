"""Side-by-side chart-range comparisons of independently completed IQ modes."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import csv
import gzip
import json
import math
from pathlib import Path
import shutil
import statistics

from .registry import IntegrityError, digest

REVISION = 'chart_range_ab_v3'


def stat(values):
    finite = [float(x) for x in values if isinstance(x, (int, float))
              and not isinstance(x, bool) and math.isfinite(x)]
    return {'mean': statistics.fmean(finite) if finite else None,
            'sample_std': statistics.stdev(finite) if len(finite) > 1 else None,
            'count': len(finite), 'total': len(values), 'unavailable_count': len(values)-len(finite)}


def paired(a, b):
    return {'a': a, 'b': b, 'delta_b_minus_a': b['mean']-a['mean']
            if a['mean'] is not None and b['mean'] is not None else None}


def independence(a, b):
    sets = [[c for role in ('development_captures', 'validation_captures')
             for c in report.get('provenance', {}).get(role, [])] for report in (a, b)]
    if not all(sets):
        return {'status': 'unavailable', 'reason': 'legacy report lacks capture provenance'}
    keys = ('id', 'raw_sha256', 'isp_sha256', 'native_pixel_sha256', 'isp_pixel_sha256')
    for key in keys:
        left, right = ({c.get(key) for c in captures} for captures in sets)
        if None in left | right or left & right:
            raise ValueError('Mode A/B requires independent capture IDs, original files and pixels: '+key)
    return {'status': 'verified', 'all_mode_capture_counts': list(map(len, sets)),
            'capture_original_pixel_sets_disjoint': True, 'paired_exposures': False,
            'interpretation': 'independent mode distributions; no paired-frame significance test'}


def native_mtf(service, report):
    """Read the exact-owner original SFR measurements, not fitted replay values."""
    from . import calibration_lab as lab, esfr_lab
    evidence = report.get('mtf_evidence', {})
    identity = evidence.get('validation_id')
    if not identity:
        return {'status': 'unavailable', 'rows': [], 'reason': 'legacy MTF evidence unavailable'}
    run = lab.get(service, identity)
    if run['status'] != 'succeeded' or digest(run['result']) != evidence['validation_result_sha256']:
        raise IntegrityError('A/B source MTF validation result changed')
    relative = 'esfr-images/full-result.json.gz'
    checksum = run['result']['summary']['files'].get(relative)
    if not checksum:
        raise IntegrityError('A/B source native SFR artifact missing')
    binding = service.registry.artifact_binding(checksum, owner_kind='runs', owner_id=identity)
    with gzip.open(binding['path'], 'rt', encoding='utf-8') as stream:
        result = json.load(stream)
    allowed = {c['id'] for c in report['provenance']['validation_captures']}
    first = report['provenance']['validation_captures'][0]
    capture_record = service.registry.get(esfr_lab.CAPTURES, first['id'])
    if capture_record['content_hash'] != first['content_hash']:
        raise IntegrityError('A/B acquisition declaration differs from original capture')
    observations = [o for o in result['observations'] if o['role'] == 'validation']
    if {o['capture_id'] for o in observations} != allowed:
        raise IntegrityError('A/B native MTF captures differ from frozen IQ validation')
    rows = []
    for capture in observations:
        for roi in capture['rois']:
            if roi.get('kind') != 'edge':
                continue
            for domain, channels in [('raw', ('R','Gr','Gb','B')), ('isp', ('R','G','B','Y'))]:
                for channel in channels:
                    metric = roi.get(domain, {}).get(channel, {})
                    rows.append({'capture_id': capture['capture_id'], 'roi_id': roi['id'],
                        'domain': domain, 'channel': channel, 'box': roi['box'],
                        'mtf50': metric.get('mtf50'), 'mtf50_reason': metric.get('mtf50_reason'),
                        'overshoot': metric.get('overshoot'), 'undershoot': metric.get('undershoot'),
                        'status': metric.get('status', 'unavailable'),
                        'nyquist_cy_native_px': metric.get('method_details', {}).get('nyquist_cy_native_px'),
                        'unit': 'cycles/native_sensor_pixel'})
    edge_ids = {r['roi_id'] for r in rows}
    if len(edge_ids) != 112 or len(observations) != 3 or len(rows) != 112*3*8:
        raise IntegrityError('A/B native MTF requires all 112 edges, three independent repeats and eight channels')
    return {'status': 'available', 'rows': rows, 'edge_count': 112, 'capture_count': 3,
            'source_run_id': identity, 'source_file': relative, 'source_sha256': checksum,
            'source_result_sha256': evidence['validation_result_sha256'],
            'synthetic_acquisition': deepcopy(capture_record['metadata'].get('synthetic_generator')),
            'isp_acceptance_scope': result['validation']['scope']}


def _groups(rows, keys):
    result = defaultdict(list)
    for row in rows:
        result[tuple(row.get(k) for k in keys)].append(row)
    return result


def assemble(a, b, native_a, native_b):
    """Keep missing values missing and report availability per mode and metric."""
    output = {'schema': 'camera_esfr_iq_ab_tables_v1', 'independence': independence(a, b),
              'judgement': 'pending_criteria', 'physical_validation': 'not_established',
              'color': [], 'noise': [], 'tone': [], 'mtf': [],
              'native_mtf': {'a': {k:v for k,v in native_a.items() if k != 'rows'},
                             'b': {k:v for k,v in native_b.items() if k != 'rows'}}}
    color_groups = [_groups(r['validation'].get('color', {}).get('patches', []), ('patch_id',)) for r in (a,b)]
    for key in sorted(set(color_groups[0]) | set(color_groups[1])):
        row = {'patch_id': key[0], 'metrics': {}}
        for stage in ('before','wb_only','after','actual_isp'):
            for metric in ('delta_e00','linear_rgb_rmse'):
                stats = [stat([(r.get(stage) or {}).get(metric) if r.get('qc', {}).get('fit_eligible') else None
                               for r in group.get(key, [])]) for group in color_groups]
                row['metrics'][stage+'_'+metric] = paired(*stats)
        row['exclusions'] = [[{'capture_id':r.get('capture_id'), 'reasons':r.get('qc', {}).get('exclusion_reasons', [])}
                              for r in group.get(key, []) if not r.get('qc', {}).get('fit_eligible')] for group in color_groups]
        output['color'].append(row)
    noise_groups = [_groups(r.get('noise_repeat_analysis', {}).get('validation', {}).get('noise', []),
                            ('patch_id','domain','channel')) for r in (a,b)]
    reference = {r['id']:r['nominal_reflectance'] for r in a.get('reference', {}).get('grays', [])}
    for key in sorted(set(noise_groups[0]) | set(noise_groups[1])):
        if not str(key[0]).startswith('gray-'):
            continue
        row = dict(zip(('patch_id','domain','channel'), key))
        row.update(nominal_reflectance=reference.get(key[0]), metrics={})
        for metric in ('mean_signal','temporal_std','temporal_variance','snr_db','frame_count'):
            row['metrics'][metric] = paired(*(stat([r.get(metric) for r in group.get(key, [])]) for group in noise_groups))
        row['unit'] = 'DN' if key[1]=='raw' else 'decoded_relative_output'
        row['variance_unit'] = row['unit']+'^2'
        row['repeat_frames_a'] = row['metrics']['frame_count']['a']['mean']
        row['repeat_frames_b'] = row['metrics']['frame_count']['b']['mean']
        output['noise'].append(row)
    tone_groups = [_groups(r['validation'].get('tone', {}).get('patches', []), ('patch_id','domain')) for r in (a,b)]
    for key in sorted(set(tone_groups[0]) | set(tone_groups[1])):
        first = next((r for group in tone_groups for r in group.get(key, [])), {})
        row = {'patch_id':key[0], 'domain':key[1], 'nominal_reflectance':reference.get(key[0],first.get('nominal_reflectance')), 'metrics':{}}
        for metric in ('observed','prediction','residual'):
            row['metrics'][metric] = paired(*(stat([r.get(metric) if r.get('qc', {}).get('fit_eligible') else None
                for r in group.get(key, [])]) for group in tone_groups))
        row['clipped_fraction'] = []
        for group in tone_groups:
            fractions = []
            for r in group.get(key, []):
                qc = r.get('qc', {})
                vals = list(qc.get('raw_clipped_fraction', {}).values()) if key[1]=='raw_green' else qc.get('isp_clipped_fraction', [])
                fractions.extend(vals)
            row['clipped_fraction'].append(stat(fractions))
        output['tone'].append(row)
    native_groups = [_groups(value.get('rows', []), ('roi_id','domain','channel')) for value in (native_a,native_b)]
    error_groups = [_groups(r.get('combined_replay', {}).get('comparisons', []), ('roi_id',)) for r in (a,b)]
    for key in sorted(set(native_groups[0]) | set(native_groups[1])):
        row = dict(zip(('roi_id','domain','channel'), key)); row['metrics'] = {}
        boxes = {tuple(r['box']) for group in native_groups for r in group.get(key, [])}
        if len(boxes) != 1:
            raise IntegrityError('A/B edge sensor-coordinate boxes differ')
        row['box'] = list(next(iter(boxes))); row['unit'] = 'cycles/native_sensor_pixel'
        for metric in ('mtf50','overshoot','undershoot'):
            row['metrics'][metric] = paired(*(stat([r.get(metric) for r in group.get(key, [])]) for group in native_groups))
        if (key[1],key[2]) in (('raw','Gr'),('isp','Y')):
            metric = 'raw_after_rmse' if key[1]=='raw' else 'isp_after_rmse'
            row['metrics']['fixed_profile_sfr_rmse'] = paired(*(stat([r.get(metric) for r in group.get((key[0],), [])]) for group in error_groups))
        output['mtf'].append(row)
    output['tone_diagnostics'] = {}
    for domain in ('raw_green','actual_isp_luma'):
        selected = sorted([r for r in output['tone'] if r['domain']==domain], key=lambda r:r['nominal_reflectance'] if r['nominal_reflectance'] is not None else -1)
        output['tone_diagnostics'][domain] = {}
        for mode in ('a','b'):
            points = [r['metrics']['observed'][mode]['mean'] for r in selected]
            output['tone_diagnostics'][domain][mode] = {'nonmonotonic_steps':sum(x>y for x,y in zip(points,points[1:]) if x is not None and y is not None),
                'available_patch_count':sum(x is not None for x in points), 'observed_patch_count':len(points),
                'extrapolated_endpoints':False, 'tone_correction_applied':False}
    return output


def copy_previews(service, a, b, directory):
    from .calibration_completion import _bindings
    directory = Path(directory); rows = []
    for mode, report in [('a',a),('b',b)]:
        files = report.get('files', {})
        names = [p for p in files if p.startswith('images/capture-01-') and p.endswith('.png')]
        if not names:
            continue
        bindings = _bindings(service, [files[p] for p in names], 'camera_esfr_iq_reports', report['id'])
        for name in names:
            destination = directory/'images'/('mode-'+mode+'-'+Path(name).name)
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(bindings[files[name]]['path'], destination)
            rows.append({'mode':mode, 'file':destination.relative_to(directory).as_posix(),
                         'source_report_id':report['id'], 'source_file':name, 'source_sha256':files[name]})
    return rows


def publish_tables(directory, payload):
    directory = Path(directory); ab = payload['ab_comparison']
    for section in ('color','noise','tone','mtf'):
        flat = []
        for row in ab[section]:
            for metric, values in row['metrics'].items():
                item = {k:(json.dumps(v, ensure_ascii=False) if isinstance(v,(list,dict)) else v)
                        for k,v in row.items() if k not in ('metrics','exclusions','clipped_fraction')}
                item.update(metric=metric, a_mean=values['a']['mean'], b_mean=values['b']['mean'],
                    delta_b_minus_a=values['delta_b_minus_a'], a_count=values['a']['count'], b_count=values['b']['count'],
                    a_unavailable=values['a']['unavailable_count'], b_unavailable=values['b']['unavailable_count'],
                    a_sample_std=values['a']['sample_std'], b_sample_std=values['b']['sample_std'],
                    exclusions=json.dumps(row.get('exclusions'),ensure_ascii=False),
                    workflow_a_id=payload['workflow_a_id'], workflow_b_id=payload['workflow_b_id'],
                    report_a_sha256=payload['report_a_sha256'], report_b_sha256=payload['report_b_sha256'],
                    source_kind=payload['source_kind'], physical_validation=payload['physical_validation'])
                item['metric_unit'] = ('dB' if metric=='snr_db' else 'frames' if metric=='frame_count'
                    else row.get('variance_unit') if metric=='temporal_variance'
                    else row.get('unit') if metric in ('temporal_std','mean_signal','mtf50')
                    else 'DeltaE00' if metric.endswith('delta_e00') else 'relative')
                flat.append(item)
        path=directory/('ab-'+section+'.csv')
        # Keep a valid empty table for legacy reports; absence is never zero.
        columns=list(dict.fromkeys(k for row in flat for k in row)) or ['metric','a_mean','b_mean','delta_b_minus_a']
        with path.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader();writer.writerows(flat)
    (directory/'ab-tables.json').write_text(json.dumps(ab,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False),encoding='utf-8')
    _plots(directory,ab)


def _plots(directory, ab):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    directory=Path(directory); (directory/'plots').mkdir(exist_ok=True)
    def save(name, curves, xlabel, ylabel):
        figure=Figure(figsize=(10,4.5),layout='constrained');FigureCanvasAgg(figure);axis=figure.subplots()
        for label,points in curves:
            finite=[(x,y) for x,y in points if x is not None and y is not None]
            if finite:axis.plot(*zip(*finite),label=label,marker='.',linewidth=1.2,markersize=4)
        axis.set(xlabel=xlabel,ylabel=ylabel);axis.grid(alpha=.2)
        if axis.lines:axis.legend()
        figure.savefig(directory/'plots'/name,dpi=140)
    colors=ab['color']
    save('color.png',[(mode.upper()+' '+stage,[(i+1,r['metrics'][stage+'_delta_e00'][mode]['mean']) for i,r in enumerate(colors)])
        for mode in ('a','b') for stage in ('actual_isp','after')],'Color patch index','Nominal-reference Delta E00')
    tone=sorted(ab['tone'],key=lambda r:r['nominal_reflectance'] if r['nominal_reflectance'] is not None else -1)
    save('tone.png',[(mode.upper()+' '+domain,[(r['nominal_reflectance'],r['metrics']['observed'][mode]['mean']) for r in tone if r['domain']==domain])
        for mode in ('a','b') for domain in ('raw_green','actual_isp_luma')],'Nominal gray reflectance','Observed response (relative)')
    noise=sorted(ab['noise'],key=lambda r:r['nominal_reflectance'] if r['nominal_reflectance'] is not None else -1)
    save('noise.png',[(mode.upper()+' '+domain+' '+channel,[(r['nominal_reflectance'],r['metrics']['snr_db'][mode]['mean']) for r in noise if (r['domain'],r['channel'])==(domain,channel)])
        for mode in ('a','b') for domain,channel in [('raw','Gr'),('isp','G')]],'Nominal gray reflectance','Repeated-frame SNR (dB)')
    mtf=ab['mtf']
    save('mtf.png',[(mode.upper()+' '+domain+' '+channel,[(i+1,r['metrics']['mtf50'][mode]['mean']) for i,r in enumerate([r for r in mtf if (r['domain'],r['channel'])==(domain,channel)])])
        for mode in ('a','b') for domain,channel in [('raw','Gr'),('isp','Y')]],'Edge index (all 112 positions)','Observed MTF50 (cycles/native pixel)')


def html_tables(payload):
    from . import esfr_iq_report as report
    ab=payload['ab_comparison']; esc=report._esc; num=report._num; table=report._table
    parts=['<section class="card"><h2>모드 A/B · 독립 검증 나란히 비교</h2><p>A: '+esc(payload['mode_a']['conditions'].get('mode_id'))+' · B: '+esc(payload['mode_b']['conditions'].get('mode_id'))+'</p><p>모드별 3개 독립 검증 프레임의 평균과 표본 표준편차입니다. B−A는 독립 분포의 기술적 차이이며 통계적 유의성이나 HDR 우위 판정이 아닙니다. 실제 ISP와 Reference ISP를 구분합니다.</p>']
    parts.append('<p><a href="report.json">전체 비교 JSON</a> · <a href="bundle.zip">A/B 보고서·CSV·그림 ZIP</a></p><p>'+' · '.join('<a href="ab-'+s+'.csv">'+s+' A/B CSV</a>' for s in ('color','noise','tone','mtf'))+'</p>')
    parts.append('<p>Noise는 반복 프레임 전체에서 계산한 시간 분산·SNR 추정값입니다. 해당 추정값의 표본 σ는 별도로 계산하지 않으며 미제공으로 표시합니다. 실제 반복 수는 repeat_frames 열에 기록합니다.</p>')
    parts.append(report._details('고정 합성 취득·DCG·ISP Tone 가정', {mode:ab['native_mtf'][mode].get('synthetic_acquisition') for mode in ('a','b')}))
    for section,title,metrics,fields in [
        ('color','18 Color 패치 · 색차',['actual_isp_delta_e00','after_delta_e00'],['patch_id']),
        ('noise','20 Gray 패치 · 시간 노이즈 / SNR',['temporal_std','snr_db'],['patch_id','domain','channel','unit','repeat_frames_a','repeat_frames_b']),
        ('tone','20 Gray 패치 · 관측 Tone 응답',['observed','residual'],['patch_id','domain','nominal_reflectance']),
        ('mtf','112개 edge · 관측 MTF50 / 보정 오차',['mtf50','fixed_profile_sfr_rmse'],['roi_id','domain','channel'])]:
        parts.append('<h2>'+title+'</h2><img style="width:100%;max-width:1100px" src="plots/'+section+'.png" alt="'+esc(title)+' A/B 그래프">')
        rows=[]
        for item in ab[section]:
            for metric in metrics:
                if metric not in item['metrics']:continue
                value=item['metrics'][metric]
                rows.append([*[esc(item.get(k,'')) for k in fields],esc(metric),num(value['a']['mean']),num(value['a']['sample_std']),num(value['b']['mean']),num(value['b']['sample_std']),num(value['delta_b_minus_a']),str(value['a']['count'])+' / '+str(value['b']['count'])])
        parts.append(table([*fields,'metric','A 평균','A 표본 σ','B 평균','B 표본 σ','B−A','A/B 유효 수'],rows))
    parts.append(report._details('독립성·Tone 단조성·native MTF 원본 hash',{'independence':ab['independence'],'tone':ab['tone_diagnostics'],'native_mtf':ab['native_mtf']}))
    parts.append('<p>RAW MTF50는 Bayer 채널의 2px 간격 표본에서 구한 native pixel 단위이며 Nyquist는 0.25 cycles/pixel입니다. ISP는 1px 간격으로 Nyquist 0.5입니다. A/B는 같은 채널끼리 비교합니다. ISP Tone 처리는 SFR·노이즈·색차에 함께 영향을 줍니다. 표준편차는 신뢰구간이 아닙니다. 계산 불가 값은 미제공으로 남깁니다.</p></section>')
    previews=payload.get('comparison_previews',[])
    if previews:
        parts.append('<section class="card"><h2>동일 차트 · 모드 A/B 영상</h2>')
        for stage in ('raw','baseline','candidate','actual_isp'):
            images=[next((r for r in previews if r['mode']==mode and r['file'].endswith('-'+stage+'.png')),None) for mode in ('a','b')]
            parts.append('<h3>'+esc(stage)+'</h3><div class="images" style="grid-template-columns:repeat(2,minmax(0,1fr))">')
            for label,image in zip(('A · '+str(payload['mode_a']['conditions'].get('mode_id')), 'B · '+str(payload['mode_b']['conditions'].get('mode_id'))),images):
                if image:parts.append('<figure><img loading="lazy" src="'+esc(image['file'])+'" alt="'+esc(label+' '+stage)+'"><figcaption>'+esc(label)+' · '+esc(stage)+'</figcaption></figure>')
            parts.append('</div>')
        parts.append('</section>')
    return ''.join(parts)

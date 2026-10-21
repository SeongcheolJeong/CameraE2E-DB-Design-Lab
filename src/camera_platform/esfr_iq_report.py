"""Static IQ evidence presentation; never fits, judges or activates a profile."""
from __future__ import annotations

import csv
import hashlib
import html
import json
import math
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

import numpy as np


def _esc(value):
    return html.escape(str(value), quote=True)


def _num(value):
    return f"{value:.6g}" if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else "미제공"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def _table(headers, rows):
    return '<div class="scroll"><table><thead><tr>' + ''.join('<th>'+_esc(h)+'</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>'+''.join('<td>'+str(v)+'</td>' for v in row)+'</tr>' for row in rows) + '</tbody></table></div>'


def _details(title, value):
    return '<details><summary>'+_esc(title)+'</summary><pre>'+_esc(_json(value))+'</pre></details>'


def _safe_link(value):
    if not isinstance(value, str) or not value.startswith('/api/v1/') or any(c in value for c in ('\\','\r','\n')):
        return None
    return value


def _report_navigation(payload):
    source = payload.get('source')
    if not isinstance(source, dict):
        return ''
    if any(not isinstance(source.get(k), str) or not source[k] or len(source[k]) > 512
           for k in ('run_id', 'candidate_id', 'condition_id')):
        return ''
    db = '/db-design.html?' + urlencode({'run':source['run_id'],
        'candidate-b':source['candidate_id'], 'condition':source['condition_id']})
    links = ['<a href="'+_esc(db)+'">DB Design Lab · 해당 조건</a>']
    workflow = payload.get('workflow_id')
    if isinstance(workflow, str) and workflow and len(workflow) <= 512:
        iq = '/esfr-measured.html?' + urlencode({'source_run':source['run_id'],
            'candidate':source['candidate_id'], 'condition':source['condition_id'], 'workflow':workflow})
        links.append('<a href="'+_esc(iq)+'">IQ 작업 열기</a>')
    return '<nav class="card" aria-label="원본 작업 연결">'+' · '.join(links)+'</nav>'


def _linear_display(array):
    value = np.clip(np.asarray(array, dtype=float), 0, 1)
    return np.where(value <= .0031308, value*12.92, 1.055*np.power(value, 1/2.4)-.055)


def _swatch(rgb):
    if not isinstance(rgb, (list, tuple)) or len(rgb) != 3:
        return '미제공'
    colors = np.round(_linear_display(rgb)*255).astype(int)
    return '<span class="swatch" style="background:rgb('+','.join(map(str, colors))+')"></span><small>'+_esc(', '.join(_num(v) for v in rgb))+'</small>'


def _plot(series, xlabel, ylabel):
    """Small inline SVG; nonfinite/missing samples are omitted, never zero-filled."""
    clean = [(label, [(float(x), float(y)) for x, y in points if isinstance(x, (int, float)) and isinstance(y, (int, float)) and math.isfinite(x) and math.isfinite(y)]) for label, points in series]
    points = [p for _, values in clean for p in values]
    if not points:
        return '<p class="muted">곡선 데이터 미제공</p>'
    xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
    ymin, ymax = min(p[1] for p in points), max(p[1] for p in points)
    if xmax == xmin: xmax = xmin+1
    if ymax == ymin: ymax = ymin+1
    yrange = ymax-ymin; ymin -= yrange*.06; ymax += yrange*.06
    x = lambda v: 65+(v-xmin)/(xmax-xmin)*690
    y = lambda v: 280-(v-ymin)/(ymax-ymin)*220
    colors = ['#da725a','#007c79','#6965b5','#906c1d','#b6488a','#527b33']
    parts = ['<svg class="plot" viewBox="0 0 800 340" role="img" aria-label="'+_esc(ylabel)+'">']
    for i in range(5):
        yy=ymin+(ymax-ymin)*i/4; xx=xmin+(xmax-xmin)*i/4
        parts.append(f'<path d="M65 {y(yy):.2f}H755" stroke="#dfe5e8"/><text x="58" y="{y(yy)+4:.2f}" text-anchor="end">{_num(yy)}</text><text x="{x(xx):.2f}" y="301" text-anchor="middle">{_num(xx)}</text>')
    for index, (label, values) in enumerate(clean):
        color=colors[index % len(colors)]
        parts.append('<polyline fill="none" stroke="'+color+'" stroke-width="1.5" points="'+' '.join(f'{x(a):.2f},{y(b):.2f}' for a,b in values)+'"/>')
        parts.extend(f'<circle cx="{x(a):.2f}" cy="{y(b):.2f}" r="2.2" fill="{color}"/>' for a,b in values)
        parts.append(f'<text x="{65+(index%3)*240}" y="{20+(index//3)*18}" fill="{color}">{_esc(label)}</text>')
    parts.append('<text x="410" y="331" text-anchor="middle">'+_esc(xlabel)+'</text><text x="17" y="180" transform="rotate(-90 17 180)" text-anchor="middle">'+_esc(ylabel)+'</text></svg>')
    return ''.join(parts)


CSS = '''body{margin:0;background:#f3f5f5;color:#1d343a;font:15px/1.6 system-ui,sans-serif}main{max-width:1420px;margin:auto;padding:36px}h1{font-size:34px;line-height:1.3}h2{margin-top:36px;font-size:23px}h3{font-size:17px}a{color:#007977}.eyebrow{letter-spacing:.12em;color:#64787c;font-size:12px}.notice{padding:18px 22px;border-left:4px solid #b57e2e;background:#fff6df}.card{background:white;border:1px solid #dae2e2;border-radius:10px;padding:20px;margin:18px 0}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border-bottom:1px solid #dce4e5;padding:10px;vertical-align:top;text-align:left}th{background:#edf2f2;white-space:nowrap}small{display:block;font-size:10px;overflow-wrap:anywhere}.swatch{display:block;width:64px;height:32px;border:1px solid #a8b7b9;margin-bottom:4px}.images{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.images figure{margin:0;min-width:0}.images img{width:100%;background:#252b2d;object-fit:contain}.images figcaption{font-size:12px}.plot{width:100%;max-width:950px;background:white}.plot text{font:11px system-ui,sans-serif}.muted{color:#61787e}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.6 ui-monospace,monospace}summary{cursor:pointer;padding:12px 0}code{overflow-wrap:anywhere}button{padding:8px 12px;border:0;border-radius:5px;background:#007977;color:white}@media(max-width:800px){main{padding:16px}.images{grid-template-columns:repeat(2,minmax(0,1fr))}h1{font-size:27px}}@media print{body{background:white}main{padding:0}button{display:none}h2{break-after:avoid}.card{break-inside:auto}.images{break-inside:avoid}}'''


def _document(title, body):
    return '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+_esc(title)+'</title><style>'+CSS+'</style></head><body><main>'+body+'</main></body></html>'


def _source_label(kind):
    return {'synthetic':'합성 입력 · 소프트웨어 동작 검증', 'measured':'실측으로 선언된 입력 · 촬영자 정보 미인증'}.get(kind, '입력 출처 미확인')


def _color(evaluation, title):
    color = evaluation.get('color', {})
    rows = color.get('patches', [])
    summary = color.get('summary', {})
    parts = ['<section class="card"><h2>'+_esc(title)+' · 색 패치</h2><p>'+str(len(rows))+'개 촬영별 관측 · 고유 패치 '+str(len({r['patch_id'] for r in rows}))+'개. 요약은 QC 적격 관측만 포함하며 제외 원인도 아래에 보존합니다.</p>']
    parts.append(_table(['출력','ΔE00 평균 / 최대 / 유효 수','linear RGB RMSE 평균 / 최대 / 유효 수'], [[label, ' / '.join(_num(summary.get(stage,{}).get('delta_e00',{}).get(k)) for k in ('mean','max','count')), ' / '.join(_num(summary.get(stage,{}).get('linear_rgb_rmse',{}).get(k)) for k in ('mean','max','count'))] for stage,label in [('before','Reference ISP 보정 전'),('wb_only','고정 WB·노출 스케일만 적용'),('after','Reference ISP WB·노출·CCM 후'),('actual_isp','실제 ISP 관측')]]))
    parts.append(_plot([(label,[(i+1,r.get(stage,{}).get('delta_e00')) for i,r in enumerate(rows)]) for stage,label in [('before','Reference 전'),('wb_only','WB·노출만'),('after','Reference 후'),('actual_isp','실제 ISP')]], '촬영별 색 패치 순서', '명목 기준 ΔE00'))
    table=[]
    for r in rows:
        metrics=[]
        for stage in ('before','wb_only','after','actual_isp'):
            item=r.get(stage,{})
            metrics.append(_num(item.get('delta_e00'))+' / '+_num(item.get('linear_rgb_rmse'))+'<small>'+_esc(item.get('delta_e_status',''))+'</small>')
        qc=r.get('qc',{})
        table.append([_esc(r.get('capture_id',''))+'<small>'+_esc(r.get('patch_id',''))+'</small>', *[_swatch(r.get(k)) for k in ('target_linear_srgb','baseline_reference_rgb','wb_only_reference_rgb','candidate_reference_rgb','actual_isp_rgb')], *metrics, _esc('적격' if qc.get('fit_eligible') else '제외: '+', '.join(qc.get('exclusion_reasons',[])))])
    parts.append(_table(['촬영 / 패치','명목 목표','Reference 전','WB·노출만','Reference 후','실제 ISP','전 ΔE / RMSE','WB ΔE / RMSE','후 ΔE / RMSE','실제 ΔE / RMSE','QC'], table))
    parts.append('<p class="muted">보정 전→후는 WB·노출 스케일·CCM의 종합 효과이며 WB·노출만→후가 같은 정규화에서 CCM의 효과입니다. 색상칩만 표시용으로 sRGB 변환·범위 제한합니다. 수치 계산은 음수·1 초과 값을 그대로 유지합니다. 실제 ISP 색 공간 미확인 시 ΔE와 명목 RGB 오차는 미제공이며 관측값·공간 통계·반복 노이즈만 보존합니다.</p></section>')
    return ''.join(parts)


def _tone(evaluation, title):
    tone=evaluation.get('tone',{}); rows=tone.get('patches',[])
    parts=['<section class="card"><h2>'+_esc(title)+' · 20개 계조 응답</h2><p>명목 반사율 → 관측 응답의 전방 모델입니다. 이 곡선을 영상에 추가 적용하지 않으며 관측 범위 밖은 외삽하지 않습니다. 센서 동적 범위나 전체 카메라 DR 측정이 아닙니다.</p>']
    for domain in ('raw_green','actual_isp_luma'):
        values=[r for r in rows if r.get('domain')==domain]
        parts.append('<h3>'+_esc(domain)+'</h3>')
        parts.append(_plot([('관측', sorted([(r.get('nominal_reflectance'),r.get('observed')) for r in values])),('고정 모델 예측',sorted([(r.get('nominal_reflectance'),r.get('prediction')) for r in values]))], '명목 반사율','고정 정규화 응답'))
    parts.append(_table(['촬영','계조 패치','domain','명목 반사율','관측','고정 예측','잔차','범위 / QC'], [[_esc(r.get('capture_id','')),_esc(r.get('patch_id','')),_esc(r.get('domain','')),*[_num(r.get(k)) for k in ('nominal_reflectance','observed','prediction','residual')],_esc(r.get('status',''))+' / '+_esc(', '.join(r.get('qc',{}).get('exclusion_reasons',[])))] for r in rows]))
    neutral=evaluation.get('neutral_reference_isp',{}).get('patches',[])
    if neutral:
        parts.append('<h3>계조 패치의 WB·노출 스케일·CCM 종합 효과</h3><p>위 계조 전방 모델과 별도로 같은 RAW의 reference ISP 전후를 비교합니다.</p>')
        parts.append(_plot([('Reference 전',[(r.get('target_reflectance'),r.get('before_rgb_rmse')) for r in neutral]),('Reference 후',[(r.get('target_reflectance'),r.get('after_rgb_rmse')) for r in neutral])],'명목 반사율','중립 RGB RMSE'))
        parts.append(_table(['촬영 / 패치','명목 목표','Reference 전','Reference 후','전 RGB RMSE','후 RGB RMSE'],[[_esc(r.get('capture_id',''))+' / '+_esc(r.get('patch_id','')),_swatch([r.get('target_reflectance',0)]*3),_swatch(r.get('baseline_reference_rgb')),_swatch(r.get('candidate_reference_rgb')),_num(r.get('before_rgb_rmse')),_num(r.get('after_rgb_rmse'))] for r in neutral]))
    parts.append('</section>');return ''.join(parts)


def _noise(noise, title):
    rows=noise.get('noise',[]) if isinstance(noise,dict) else []
    parts=['<section class="card"><h2>'+_esc(title)+' · 명시적으로 선택한 반복 노이즈</h2>']
    if not rows:
        parts.append('<p>반복 집합 미제공 또는 분석 불가: '+_esc(noise.get('reason',noise.get('status','미제공')) if isinstance(noise,dict) else '미제공')+'. 시간 노이즈를 0으로 표시하지 않습니다.</p>')
    else:
        for domain in ('raw','isp'):
            series=[]
            for channel in ('R','Gr','Gb','G','B'):
                values=[(r.get('mean_signal'),r.get('temporal_std')) for r in rows if r.get('domain')==domain and r.get('channel')==channel]
                if values:series.append((domain+' '+channel,values))
            parts.append('<h3>'+domain+' · '+('DN' if domain=='raw' else 'decoded relative output')+'</h3>'+_plot(series,'패치 평균 신호','시간 표준편차'))
        parts.append(_table(['패치','domain / 채널','프레임','평균','시간 σ','시간 분산','SNR dB','단위','상태 / 불확도'], [[_esc(r.get('patch_id','')),_esc(str(r.get('domain',''))+' '+str(r.get('channel',''))),*[_num(r.get(k)) for k in ('frame_count','mean_signal','temporal_std','temporal_variance','snr_db')],_esc(r.get('unit','')),_esc(str(r.get('status',''))+' / '+str(r.get('uncertainty','')))] for r in rows]))
    parts.append('<p class="muted">고정된 ROI의 반복 변동에는 광원 드리프트가 포함됩니다. Dark/PTC의 read noise·gain·full well 추정이 아닙니다. RAW DN과 ISP 상대 출력은 단위가 다릅니다.</p></section>');return ''.join(parts)


def _observations(payload):
    rows=[]
    for capture in payload.get('numerical',{}).get('observations',{}).get('captures',[]):
        for patch in capture.get('patches',[]):
            for domain in ('raw','isp'):
                for channel,stats in patch.get(domain,{}).items():
                    if not isinstance(stats,dict):continue
                    rows.append([_esc(capture.get('capture_id','')),_esc(patch.get('id','')),_esc(domain+' '+channel),*[_num(stats.get(k)) for k in ('mean','spatial_std','spatial_variance','sample_count')],_esc(', '.join(patch.get('qc',{}).get('exclusion_reasons',[])))])
    return '<details class="card"><summary>전체 38개 패치의 RAW 4채널·ISP 3채널 공간 통계 ('+str(len(rows))+'개 채널 관측)</summary><p>공간 표준편차에는 차트 질감·조명 불균일·고정 패턴이 섞입니다. 위 반복 시간 노이즈와 구분합니다.</p>'+_table(['촬영','패치','domain / 채널','평균','공간 σ','공간 분산','표본 수','QC 제외 사유'],rows)+'</details>'


def _image_section(directory, images):
    parts=['<section class="card"><h2>동일 촬영의 전체 차트 영상</h2><p>RAW와 실제 ISP는 원본에서 만든 표시용 축소 영상입니다. Reference ISP는 native Bayer에서 bilinear 처리 후 같은 RAW에 고정 WB·노출·CCM을 적용한 영상입니다. 정량 측정은 native ROI에서 수행하며 이 미리보기로 다시 계산하지 않습니다. Tone 곡선은 적용하지 않았습니다.</p>']
    if not images:parts.append('<p class="notice">차트 영상 미제공. 색상칩 표를 실제 촬영 영상으로 대체하지 않았습니다.</p>')
    for item in images or []:
        parts.append('<h3>'+_esc(item.get('capture_id',''))+'</h3><div class="images">')
        for key,label in [('raw','입력 RAW · 표시용'),('baseline','Reference ISP · 보정 전'),('candidate','Reference ISP · WB·노출·CCM 후'),('actual_isp','실제 ISP · 관측')]:
            relative=item.get('files',{}).get(key)
            if relative:
                path=PurePosixPath(relative)
                if path.is_absolute() or '..' in path.parts or '\\' in relative or not (directory / relative).is_file() or (directory / relative).is_symlink():
                    raise ValueError('Unsafe or missing IQ preview artifact')
                parts.append('<figure><a href="'+_esc(relative)+'"><img loading="lazy" src="'+_esc(relative)+'" alt="'+_esc(label)+'"></a><figcaption>'+_esc(label)+'</figcaption></figure>')
        parts.append('</div><small>동일 RAW SHA256: '+_esc(item.get('raw_sha256','미제공'))+' · '+_esc(item.get('method',''))+'</small>')
    parts.append('</section>');return ''.join(parts)


def _combined(value):
    if not value:return ''
    metric=value.get('isp_sfr_rmse',{})
    rows=value.get('comparisons',[])
    parts=['<section class="card"><h2>고정 유효 MTF + WB·노출·CCM 통합 재실행</h2><p>'+_esc(value.get('edge_count','미제공'))+'개 edge · '+_esc(value.get('comparison_count','미제공'))+'개 독립 촬영 비교. 저장된 native MTF 재실행 배열에 고정 CCM을 적용했습니다. DB 렌즈 처방을 다시 계산한 결과가 아니며 CCM은 RAW를 바꾸지 않습니다. IQ 승인이나 자동 활성화 판정은 추가하지 않습니다.</p>']
    parts.append(_table(['ISP SFR RMSE 평균 전','평균 후','최대 후','유효 수','재피팅','CCM의 RAW 변경'], [[_num(metric.get(k)) for k in ('before','after','worst_after','count')]+[_esc(value.get('refit_performed','미제공')),_esc(value.get('raw_changed_by_ccm','미제공'))]]))
    parts.append(_plot([('통합 재실행 전',[(i+1,r.get('isp_before_rmse')) for i,r in enumerate(rows)]),('통합 재실행 후',[(i+1,r.get('isp_after_rmse')) for i,r in enumerate(rows)])],'촬영별 edge 순서','ISP SFR RMSE'))
    parts.append('<details><summary>전체 '+str(len(rows))+'개 edge 관측 오차</summary>'+_table(['촬영','edge','RAW 전','RAW 후','ISP 전','ISP 후','ISP 상태'], [[_esc(r.get('capture_id','')),_esc(r.get('roi_id','')),*[_num(r.get(k)) for k in ('raw_before_rmse','raw_after_rmse','isp_before_rmse','isp_after_rmse')],_esc(r.get('isp_threshold_status',''))] for r in rows])+'</details>')
    parts.append(_details('기존 RAW 판정 근거 (기준 유지)',value.get('raw_gate_unchanged',{})))
    parts.append('</section>');return ''.join(parts)


def render_chart_previews(captures, profile, directory):
    """Render one source pair at a time, preserving native hashes and pixel roles."""
    from PIL import Image
    from . import esfr_iq_engine as engine
    from .esfr_io import read_isp
    from .registry import IntegrityError
    directory=Path(directory);out=directory/'images';out.mkdir(parents=True,exist_ok=True)
    result=[]
    for index,capture in enumerate(captures):
        record=capture['record']; preview=engine.render_reference_preview(capture,profile)
        isp_path=Path(capture['isp_path']); expected=record['files']['isp']['sha256']
        sha=hashlib.sha256()
        with isp_path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024),b''):sha.update(chunk)
        if sha.hexdigest()!=expected:raise IntegrityError('IQ preview ISP source hash differs')
        actual,info=read_isp(isp_path,record['metadata']['isp'])
        if list(actual.shape[:2])!=preview['native_shape']:raise IntegrityError('IQ preview native RAW/ISP geometry differs')
        h,w=preview['preview_shape'];ri=np.linspace(0,actual.shape[0]-1,h).astype(int);ci=np.linspace(0,actual.shape[1]-1,w).astype(int)
        actual_small=actual[ri[:,None],ci[None,:]];del actual
        arrays={'raw':preview.pop('normalized_raw_preview'),'baseline':preview.pop('baseline_linear_rgb'),'candidate':preview.pop('candidate_linear_rgb'),'actual_isp':actual_small}
        paths={}
        for kind,array in arrays.items():
            encoded=_linear_display(array) if kind!='actual_isp' or info.get('linearized', info.get('linear',record['metadata']['isp'].get('transfer') in ('linear','srgb'))) else np.clip(array,0,1)
            image=Image.fromarray(np.round(encoded*255).astype(np.uint8));name=f'images/capture-{index+1:02d}-{kind}.png';image.save(directory/name);paths[kind]=name
        result.append({'capture_id':record['id'],**preview,'isp_sha256':expected,'files':paths,'actual_isp_decode':info,'display_clipping_only':True})
    return result


def _write(directory, payload, document, images=None):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    (directory/'report.html').write_text(document,encoding='utf-8')
    (directory/'iq-result.json').write_text(_json(payload),encoding='utf-8')
    if images is not None:(directory/'preview-manifest.json').write_text(_json(images),encoding='utf-8')
    return {'report_file':'report.html','files':[p.relative_to(directory).as_posix() for p in sorted(directory.rglob('*')) if p.is_file()],'image_count':sum(len(r.get('files',{})) for r in images or [])}


CSV_FILES = ('color-patches.csv', 'gray-tone.csv', 'noise-patches.csv', 'provenance.csv')


def _csv_cell(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list, tuple, bool)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return value


def _csv_rows(directory, name, columns, rows):
    with (directory/name).open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='raise')
        writer.writeheader()
        writer.writerows({key:_csv_cell(row.get(key)) for key in columns} for row in rows)


def _publish_csv(directory, payload):
    """Lossless diagnostic tables; absent metrics stay blank, never become zero."""
    source, profile = payload.get('source', {}), payload.get('profile', {})
    reference, provenance = payload.get('reference', {}), payload.get('provenance', {})
    export = payload.get('export_provenance', {})
    bindings = {c['id']:c for role in ('development','validation')
                for c in provenance.get(role+'_captures', []) if c.get('id')}
    base = {'workflow_id':payload.get('workflow_id'), 'source_kind':payload.get('source_kind'),
            'run_id':source.get('run_id'), 'candidate_id':source.get('candidate_id'), 'condition_id':source.get('condition_id'),
            'reference_id':reference.get('id'), 'reference_status':reference.get('status'), 'reference_source':reference.get('source'),
            'reference_sha256':reference.get('sha256'), 'reference_document_sha256':provenance.get('reference_sha256'),
            'profile_self_sha256':profile.get('profile_sha256'), 'profile_document_sha256':provenance.get('profile_sha256'),
            'patch_roi_sha256':provenance.get('patch_roi_sha256'), 'physical_validation':payload.get('physical_validation'),
            'validation_fit_performed':provenance.get('validation_fit_performed'),
            'source_report_id':export.get('source_report_id'), 'source_report_sha256':export.get('source_report_sha256'),
            'export_renderer_sha256':export.get('renderer_sha256')}
    common = [*base, 'role', 'capture_id', 'patch_id', 'raw_sha256', 'isp_sha256',
              'observation_sha256', 'independent_of_training', 'same_raw_before_after',
              'fit_eligible', 'exclusion_reasons', 'qc_json']
    def row_context(role, evaluation, row):
        capture = bindings.get(row.get('capture_id'), {})
        qc = row.get('qc') or {}
        return {**base, 'role':role, 'capture_id':row.get('capture_id'), 'patch_id':row.get('patch_id', row.get('id')),
                'raw_sha256':row.get('raw_sha256', capture.get('raw_sha256')), 'isp_sha256':capture.get('isp_sha256'),
                'observation_sha256':evaluation.get('observation_sha256'),
                'independent_of_training':evaluation.get('independent_of_training'),
                'same_raw_before_after':row.get('same_raw_before_after', evaluation.get('same_raw_before_after')),
                'fit_eligible':qc.get('fit_eligible'), 'exclusion_reasons':qc.get('exclusion_reasons'), 'qc_json':qc or None}
    color, gray, noise = [], [], []
    stage_rgb = {'before':'baseline_reference_rgb','wb_only':'wb_only_reference_rgb',
                 'after':'candidate_reference_rgb','actual_isp':'actual_isp_rgb'}
    metrics = ['delta_e00','linear_rgb_rmse','max_abs_component_error','delta_e_status',
               'matrix_outputs_clipped','negative_components','above_one_components','lab_d65']
    for role in ('development','validation'):
        evaluation = payload.get(role) or (payload.get('numerical', {}).get(role) or {})
        for row in evaluation.get('color', {}).get('patches', []):
            for stage, rgb_key in stage_rgb.items():
                metric = row.get(stage) or {}
                color.append({**row_context(role,evaluation,row), 'stage':stage, 'stage_available':bool(metric),
                    'target_linear_srgb':row.get('target_linear_srgb'), 'nominal_lab_d50':row.get('nominal_lab_d50'),
                    'output_rgb':row.get(rgb_key), 'output_domain':'decoded_actual_isp' if stage=='actual_isp' else 'linear_reference_isp', **{key:metric.get(key) for key in metrics}})
        for row in evaluation.get('tone', {}).get('patches', []):
            gray.append({**row_context(role,evaluation,row), 'analysis':'forward_response',
                **{key:row.get(key) for key in ('domain','nominal_reflectance','observed','before','prediction','residual','status')}})
        for row in evaluation.get('neutral_reference_isp', {}).get('patches', []):
            gray.append({**row_context(role,evaluation,row), 'analysis':'reference_isp_neutral',
                'domain':'reference_linear_rgb', 'nominal_reflectance':row.get('target_reflectance'),
                **{key:row.get(key) for key in ('baseline_reference_rgb','candidate_reference_rgb','before_rgb_rmse','after_rgb_rmse')}})
        repeated = payload.get('noise_repeat_analysis', {}).get(role) or {}
        capture_rows = repeated.get('capture_bindings') or []
        for row in repeated.get('noise', []):
            noise.append({**row_context(role,evaluation,row), 'statistic_kind':'temporal_repeat',
                'observation_sha256':repeated.get('observation_sha256'),
                'variance_unit':'DN^2' if row.get('unit')=='DN' else 'decoded_relative_output^2' if row.get('unit')=='decoded_relative_output' else None,
                **{key:row.get(key) for key in ('domain','channel','frame_count','mean_signal','temporal_std','temporal_variance',
                    'snr','snr_db','unit','status','uncertainty','interpretation')},
                'capture_ids':[c.get('capture_id') for c in capture_rows],
                'raw_sha256_set':[c.get('raw_sha256') for c in capture_rows],
                'isp_sha256_set':[c.get('isp_sha256') for c in capture_rows]})
    # Native per-capture spatial statistics are separate from repeated-frame noise.
    observations = payload.get('numerical', {}).get('observations') or {}
    for capture in observations.get('captures', []):
        role = capture.get('role'); evaluation = payload.get(role, {}) if role else {}
        for patch in capture.get('patches', []):
            for domain in ('raw','isp'):
                for channel, stats in (patch.get(domain) or {}).items():
                    if not isinstance(stats, dict):
                        continue
                    noise.append({**row_context(role,evaluation,{**patch,'capture_id':capture.get('capture_id')}),
                        'statistic_kind':'spatial_per_capture','domain':domain,'channel':channel,
                        'mean_signal':stats.get('mean'),'spatial_std':stats.get('spatial_std'),
                        'spatial_variance':stats.get('spatial_variance'),'sample_count':stats.get('sample_count'),
                        'unit':'DN' if domain=='raw' else 'decoded_relative_output',
                        'variance_unit':'DN^2' if domain=='raw' else 'decoded_relative_output^2',
                        'interpretation':'spatial_variation_includes_chart_texture_and_nonuniformity_not_temporal_noise'})
    _csv_rows(directory, CSV_FILES[0], common+['stage','stage_available','target_linear_srgb','nominal_lab_d50','output_rgb','output_domain']+metrics, color)
    _csv_rows(directory, CSV_FILES[1], common+['analysis','domain','nominal_reflectance','observed','before','prediction','residual','status',
        'baseline_reference_rgb','candidate_reference_rgb','before_rgb_rmse','after_rgb_rmse'], gray)
    _csv_rows(directory, CSV_FILES[2], common+['statistic_kind','domain','channel','frame_count','mean_signal','temporal_std','temporal_variance',
        'snr','snr_db','spatial_std','spatial_variance','sample_count','unit','variance_unit','status','uncertainty','interpretation',
        'capture_ids','raw_sha256_set','isp_sha256_set'], noise)
    provenance_rows = []
    def flatten(section, value, path=''):
        if isinstance(value, dict) and value:
            for key in sorted(value):
                flatten(section,value[key],path+'.'+str(key) if path else str(key))
        else:
            kind = 'null' if value is None else 'boolean' if isinstance(value,bool) else type(value).__name__
            provenance_rows.append({'section':section,'field':path,'value_type':kind,
                'value_json':json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)})
    sections = {'source':source,'reference':reference,'profile':profile,'numerical_provenance':provenance,
        'workflow':{k:payload.get(k) for k in ('workflow_id','source_kind','physical_validation','physical_identity_verified',
            'composite_candidate_sha256','db_active_model_updated')},
        'evaluation':{role:{k:(payload.get(role) or {}).get(k) for k in ('status','role','refit_performed','independent_of_training',
            'same_raw_before_after','observation_sha256','profile_sha256','raw_sha256')} for role in ('development','validation')},
        'noise_protocol':payload.get('noise_repeat_analysis',{}).get('protocol'),
        'export_provenance':export}
    for section, value in sections.items():
        flatten(section,value)
    _csv_rows(directory, CSV_FILES[3], ['section','field','value_type','value_json'], provenance_rows)
    return {name:len(rows) for name,rows in zip(CSV_FILES,(color,gray,noise,provenance_rows))}


def publish(directory, payload, images=None):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    csv_counts=_publish_csv(directory,payload)
    evaluation=payload.get('validation',payload.get('numerical',{}).get('validation',{}));development=payload.get('development',{})
    parts=['<p class="eyebrow">CAMERAE2E / E-SFR / IQ EVIDENCE</p><h1>112 Edge와 색·계조 IQ 통합 보고서</h1><p>'+_esc(_source_label(payload.get('source_kind')))+' · '+_esc(payload.get('workflow_id',''))+'</p><p><button onclick="window.print()">인쇄 / PDF 저장</button> · <a href="iq-result.json">전체 정량 결과 JSON</a> · <a href="bundle.zip">후보·근거 ZIP</a></p><div class="notice"><b>IQ 판정 대기 · 명목 기준의 조건부 후보 · DB 자동 활성화 없음</b><p>18색·20계조의 제조사 설계 명목값에 대한 진단입니다. 실물 차트 반사율·광원 SPD·센서 QE 또는 제조사 ISP를 식별한 결과가 아닙니다. 물리 검증과 ISO 적합성은 확립되지 않았습니다. 실제 ISP는 관측만 하며 변경하지 않습니다.</p></div>']
    parts.append(_report_navigation(payload))
    parts.append('<section class="card"><h2>패치별 CSV 다운로드</h2><p>개발·검증과 모든 출력 단계를 보존합니다. 빈 수치 칸은 미제공이며 0이 아닙니다. JSON 배열·QC 제외 사유·기준 및 프로파일 hash를 포함합니다.</p><ul>'+''.join('<li><a href="'+name+'">'+name+'</a> · '+str(csv_counts[name])+'행</li>' for name in CSV_FILES)+'</ul></section>')
    if payload.get('export_provenance'):
        parts.append('<section class="card"><h2>보고서 재발행 근거</h2><p>원 수치 계산의 코드·후보 hash는 유지하고 보고서 생성 코드만 별도로 기록합니다. 재피팅·수치 재실행 여부는 아래 영수증을 따릅니다.</p>'+_details('원 수치 출처와 render/export 출처',payload['export_provenance'])+'</section>')
    provenance=payload.get('provenance',{})
    parts.append('<section class="card"><h2>자료 분리와 적용 범위</h2>'+_table(['항목','기록'],[[key,_esc(value)] for key,value in [('개발 전용 피팅','WB·노출 스케일·3×3 CCM 및 계조 전방 모델'),('검증 재피팅',provenance.get('validation_fit_performed','미제공')),('동일 RAW의 CCM 전후',evaluation.get('same_raw_before_after','미제공')),('검증 독립성',evaluation.get('independent_of_training','미제공')),('후보 고정',provenance.get('frozen_at','미제공')),('새 취득 계약',provenance.get('contract_created_at','미제공')),('촬영 시각 인증',provenance.get('acquisition_time_authenticated','미제공')),('합격 기준','미설정; IQ judgement pending')]])+'</section>')
    parts.append(_image_section(directory,images))
    parts.extend([_color(evaluation,'새 독립 검증'),_tone(evaluation,'새 독립 검증'),_noise(payload.get('noise_repeat_analysis',{}).get('validation',{}),'새 독립 검증')])
    parts.append(_observations(payload))
    parts.append('<details><summary>개발 자료 결과 · 독립 검증과 분리</summary>'+_color(development,'개발 피팅')+_tone(development,'개발 피팅')+_noise(payload.get('noise_repeat_analysis',{}).get('development',{}),'개발')+'</details>')
    parts.append(_combined(payload.get('combined_replay')))
    mtf=payload.get('mtf_evidence',{});links=[]
    for key,label in [('report_url','112개 RAW·ISP 영상/SFR 보고서'),('completion_report_url','기존 112개 보정·검증 보고서')]:
        link=_safe_link(mtf.get(key))
        if link:links.append('<a href="'+_esc(link)+'">'+label+'</a>')
    parts.append('<section class="card"><h2>DB 연결과 112개 근거</h2><p>'+' · '.join(links)+'</p>'+_details('정확한 DB 실행·후보·조건',payload.get('source',{}))+_details('고정 IQ 파라미터',payload.get('profile',{}))+_details('복합 후보 (비활성 저장)',payload.get('composite_candidate',{}))+_details('원본·프로파일·기준·코드 hash / 취득 계약',provenance)+_details('버전 고정 명목 기준',payload.get('reference',{}))+'</section>')
    parts.append('<section class="card"><h2>해석의 한계</h2><ul>'+''.join('<li>'+_esc(v)+'</li>' for v in payload.get('limitations',[]))+'</ul></section>')
    return _write(directory,payload,_document('CameraE2E · E-SFR IQ 통합 보고서',''.join(parts)),images)


def _mode_comparison_metrics(mode):
    """Descriptive after-errors only; missing observations are never zero-filled."""
    def statistic(values, total):
        finite = [v for v in values if isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v)]
        return {'mean': sum(finite)/len(finite) if finite else None,
                'count': len(finite), 'total': max(total, len(values)),
                'unavailable_count': max(total, len(values))-len(finite)}
    color = mode.get('validation', {}).get('color', {})
    color_rows = color.get('patches') or []
    eligible = [row for row in color_rows if row.get('qc', {}).get('fit_eligible') is True]
    color_total = color.get('eligible_patch_observations')
    if type(color_total) is not int or color_total < 0:
        color_total = len(eligible)
    combined = mode.get('combined_replay') or {}
    edge_rows = combined.get('comparisons') or []
    edge_total = combined.get('comparison_count')
    if type(edge_total) is not int or edge_total < 0:
        edge_total = len(edge_rows)
    return {'color_after_delta_e00': statistic([r.get('after', {}).get('delta_e00') for r in eligible], color_total),
            'raw_after_sfr_rmse': statistic([r.get('raw_after_rmse') for r in edge_rows], edge_total),
            'combined_isp_after_sfr_rmse': statistic([r.get('isp_after_rmse') for r in edge_rows], edge_total)}


def _mode_summary(payload):
    a = _mode_comparison_metrics(payload.get('mode_a', {}))
    b = _mode_comparison_metrics(payload.get('mode_b', {}))
    rows = []
    for key, label in [('color_after_delta_e00', 'WB·노출·CCM 후 명목 색 ΔE00 평균'),
                       ('raw_after_sfr_rmse', '고정 유효 MTF 후 RAW SFR RMSE 평균'),
                       ('combined_isp_after_sfr_rmse', '유효 MTF + WB·노출·CCM 후 ISP SFR RMSE 평균')]:
        left, right = a[key], b[key]
        delta = right['mean']-left['mean'] if left['mean'] is not None and right['mean'] is not None else None
        coverage = lambda item: str(item['count'])+' / '+str(item['total'])+' (미제공 '+str(item['unavailable_count'])+')'
        rows.append([label, _num(left['mean']), _num(right['mean']), _num(delta), coverage(left), coverage(right), '기준 미설정 · 판정 대기'])
    return '<section class="card"><h2>A/B 정량 요약 · B−A</h2><p>각 모드의 새 독립 검증을 집계합니다. MTF 값은 combined replay의 촬영별 edge 관측 행에서 직접 계산하며 저장된 요약값이나 0으로 대체하지 않습니다. 양수는 B의 오차가 큰 방향입니다. 가용 관측 수가 다르면 평균 차이에 누락의 영향이 포함될 수 있으며, 짝지은 통계 검정이나 HDR 우위 판정이 아닙니다.</p>'+_table(['보정 후 지표','A','B','B−A','A 유효 / 대상','B 유효 / 대상','판정'], rows)+'</section>'


def _mode_mtf(mode, label):
    evidence = mode.get('mtf_evidence') or {}
    combined = mode.get('combined_replay') or {}
    parts = ['<section class="card"><h2>'+_esc(label)+' · 차트 범위 MTF 근거</h2>']
    for key, name in [('report_url', '112개 RAW·ISP 영상/SFR 원본 보고서'),
                      ('completion_report_url', '보정·검증 원본 보고서')]:
        link = _safe_link(evidence.get(key))
        if link:
            parts.append('<p><a href="'+_esc(link)+'">'+_esc(label+' '+name)+'</a></p>')
    if not evidence and not combined:
        parts.append('<p class="notice">MTF 근거 미제공. 오차를 0으로 대체하지 않습니다.</p>')
    retained=combined.get('retained_curves') or {}
    link=_safe_link(retained.get('url'))
    if link:
        parts.append('<p><a href="'+_esc(link)+'">'+_esc(label)+' 원 native 재실행 곡선 JSON</a> · SHA256 <code>'+_esc(retained.get('source_file_sha256'))+'</code></p>')
    parts.append(_details('해당 모드의 MTF 판정·검증 ID·hash', evidence))
    parts.append('</section>')
    parts.append(_combined(combined))
    return ''.join(parts)


def _mode_gate_summary(directory, payload):
    """Expose existing MTF failures without turning IQ pending into MTF passing."""
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    rows=[];failed=[];all_checks=[]
    for mode,label in [('mode_a','A'),('mode_b','B')]:
        gate=(payload.get(mode,{}).get('mtf_evidence') or {}).get('image_validation') or {}
        checks=gate.get('checks')
        if not isinstance(checks,list):
            rows.append([label,'미제공',*['미제공']*7]);continue
        status_counts={status:sum(r.get('status')==status for r in checks) for status in ('pass','fail','diagnostic','unavailable')}
        thresholds={r['metric']:r.get('threshold') for r in checks}
        rows.append([label,_esc(gate.get('status','미제공')),_esc(gate.get('scope','미제공')),
            _num(thresholds.get('raw_sfr_rmse')),_num(thresholds.get('isp_sfr_rmse')),
            *[str(status_counts[k]) for k in ('pass','fail','diagnostic','unavailable')]])
        for check in checks:
            record={'mode':label,**check};all_checks.append(record)
            if check.get('status')=='fail':failed.append(record)
    path=Path(directory)/'ab-mtf-acceptance.csv'
    columns=list(dict.fromkeys(k for r in all_checks for k in r)) or ['mode','metric','status','value','threshold']
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader();writer.writerows(all_checks)
    parts=['<section class="card"><h2>MTF 기존 기준 판정 · A/B</h2>']
    if failed:
        parts.append('<div class="notice"><strong>MTF 기준 초과 '+str(len(failed))+'건 · 실패를 보존합니다.</strong><p>평균 개선은 전체 검증 통과를 의미하지 않습니다. 원본 각 모드의 RAW·matched ISP 기준을 그대로 유지합니다. 실제 ISP diagnostic 결과와 IQ 기준 미선언 상태는 MTF 통과 건수에 포함하지 않습니다.</p></div>')
    parts.append(_table(['모드','MTF 판정','적용 범위','RAW 기준','matched ISP 기준','통과','실패','진단','미제공'],rows))
    parts.append('<p><a href="ab-mtf-acceptance.csv">전체 촬영별 MTF 판정 CSV</a> · Color·Noise·Tone의 합격 판정은 기준 미선언으로 보류합니다.</p>')
    if failed:
        parts.append('<h3>기준 초과 위치와 촬영</h3>'+_table(['모드','촬영','edge','지표','값','기준'],[
            [_esc(r.get('mode')),_esc(r.get('capture_id')),_esc(r.get('roi_id')),_esc(r.get('metric')),_num(r.get('value')),_num(r.get('threshold'))] for r in failed]))
    parts.append('</section>');return ''.join(parts)


def publish_comparison(directory, payload):
    parts=['<p class="eyebrow">CAMERAE2E / FIXED CHART RANGE COMPARISON</p><h1>모드 A/B · 동일 차트 범위 비교</h1><p>'+_esc(_source_label(payload.get('source_kind')))+' · IQ 판정 대기</p><div class="notice">각 모드는 독립적으로 고정된 후보와 검증을 유지합니다. 입력 모드는 별도의 취득기에서 생성합니다. 비교 엔진은 입력 영상의 다중 노출 HDR 복원을 수행하지 않습니다. 합성 DCG 조건은 원본 선언에 기록됩니다. 이 비교는 카메라 전체 동적 범위 측정이 아닙니다.</div>'+_details('조건 차이',payload.get('condition_differences',{}))]
    parts.append(_mode_gate_summary(directory,payload))
    parts.append(_mode_summary(payload))
    if payload.get('ab_comparison'):
        from .esfr_iq_ab import html_tables
        parts.append(html_tables(payload))
    for key,label,noise_key in [('mode_a','모드 A','noise_a'),('mode_b','모드 B','noise_b')]:
        mode=payload.get(key,{})
        parts.append('<h2>'+label+'</h2>'+_details('DB와 촬영 조건',{'source':mode.get('source'),'conditions':mode.get('conditions')}))
        parts.append(_report_navigation({'source':mode.get('source'), 'workflow_id':payload.get('workflow_a_id' if key=='mode_a' else 'workflow_b_id')}))
        link=_safe_link(mode.get('report_url'))
        if link:parts.append('<p><a href="'+_esc(link)+'">'+label+' 전체 영상·검증 보고서</a></p>')
        parts.append(_mode_mtf(mode,label))
        parts.extend([_color(mode.get('validation',{}),label),_tone(mode.get('validation',{}),label),_noise(payload.get(noise_key,{}).get('validation',{}),label)])
    parts.append(_details('원본 보고서 hash와 비교 한계', {k:payload.get(k) for k in
        ('comparison_id','comparison_revision','comparison_source_code','workflow_a_id','workflow_b_id',
         'report_a_sha256','report_b_sha256','reference','condition_differences','scope','source_kind',
         'physical_validation','camera_dynamic_range_measured','limitations')}))
    return _write(directory,payload,_document('CameraE2E · 모드 A/B IQ 비교',''.join(parts)))

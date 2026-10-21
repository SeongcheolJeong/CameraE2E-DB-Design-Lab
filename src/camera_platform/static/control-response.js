/* Read-only presentation of the preserved common RAW/control response analysis. */
'use strict';
window.CameraResponseObservationReport = function(report, {esc, number}) {
  const comparison = report.response_comparison;
  if (!comparison) return '';
  const predicted = comparison.prediction, observed = comparison.measurement;
  const spec = comparison.specification;
  const states = {
    valid: '관측됨', missing_raw: 'RAW 누락', insufficient_common_cells: '유효 Bayer 셀 부족',
    unknown_applied_controls: '적용 설정 미확인', no_issued_command: '명령 없음',
    unknown_command_event: '이벤트 시각 미확인', command_pending_after_sequence: '구간 이후 적용 대기',
    command_superseded: '후속 명령으로 대체', command_stale: '오래된 명령',
    command_unknown: '명령 상태 미확인', incomplete_window: '구간 관측 부족',
    not_settled_in_window: '구간 내 미정착',
  };
  const value = point => point.value === null ? '—' : number(point.value, 6);
  const status = point => states[point.status] || point.status;
  const rows = predicted.points.map((p, index) => {
    const m = observed.points[index];
    const residual = p.value === null || m.value === null ? '—' : number(p.value - m.value, 6);
    return `<tr data-response-sample="${esc(p.frame_id)}"><td>${esc(p.frame_id)}</td><td>${value(p)}<small>${esc(status(p))}</small></td><td>${value(m)}<small>${esc(status(m))}</small></td><td>${residual}</td></tr>`;
  }).join('');
  const window = predicted.response_window;
  const unit = esc(predicted.unit);
  return `<div class="inline-spacer"></div><section data-response-observation="${esc(predicted.method.specification_sha256)}"><h3>폐루프 응답 관측</h3><p class="list-note">${esc(spec.observable)} · ${esc(spec.reduction)} · 결과 단위 ${unit}</p><div class="callout neutral">시뮬레이션과 비교 RAW에 같은 관측 방법을 적용했습니다. 누락은 —로 남기며 실제 노출·Gain·WB는 비교할 결과입니다. WB 채널 비율은 전체 색 정확도를 뜻하지 않습니다.</div><p data-response-coverage>RAW 프레임: 시뮬레이션 ${predicted.coverage.raw_frames}/${predicted.coverage.planned_frames} · 비교 관측 ${observed.coverage.raw_frames}/${observed.coverage.planned_frames}<br>짝지은 표본 ${comparison.paired_samples.length}개 · 미확인 표본 ${comparison.missing_samples.length}개</p>${window?`<p class="list-note">구간 ${esc(window.start_frame_id)} → ${esc(window.end_frame_id)} · 목표 ${number(window.target,6)} ${esc(predicted.observable_unit)}${window.settling_band!==null?` · 허용 띠 ±${number(window.settling_band,6)} · 최소 ${window.minimum_dwell_frames}프레임 유지`:''}<br>정착 시간은 이 구간의 표본에 한정하며 연속 시간의 안정성 판정이 아닙니다.</p>`:''}<div class="table-wrap"><table><thead><tr><th>프레임 / 구간</th><th>시뮬레이션 (${unit})</th><th>비교 관측 (${unit})</th><th>잔차 (${unit})</th></tr></thead><tbody>${rows}</tbody></table></div><p class="muted">전체 관측·시계 정렬·요청/명령/적용 기록은 아래 판정 근거에 보존되어 있습니다.</p></section>`;
};

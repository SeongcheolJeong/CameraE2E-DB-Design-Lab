'use strict';

const FREQUENCY_TARGET_SCHEMA = 'camerae2e_lens_sensor_frequency_target_study_v1';
const FREQUENCY_TARGET_KIND = 'lens_sensor_frequency_target';

function frequencyTargetNumber(raw, label, minimum, maximum, integer = false) {
  if (typeof raw !== 'string' || raw.trim() === '') throw new Error(`${label}: 값을 입력하세요.`);
  const value = Number(raw);
  if (!Number.isFinite(value) || value < minimum || value > maximum || (integer && !Number.isInteger(value))) {
    throw new Error(`${label}: ${minimum}–${maximum} 범위의 ${integer ? '정수' : '숫자'}가 필요합니다.`);
  }
  return value;
}

function frequencyTargetBuildSpecification(template, input) {
  if (!template || template.schema !== FREQUENCY_TARGET_SCHEMA) throw new Error('서버 표본화 template 버전이 화면과 다릅니다.');
  const spec = JSON.parse(JSON.stringify(template));
  const roi = spec.registered_roi;
  const goal = spec.sampling_goal;
  if (!roi || !goal) throw new Error('서버 template에 등록 ROI 또는 목표 계약이 없습니다.');
  spec.name = String(input.name || '').trim();
  if (!spec.name || spec.name.length > 160) throw new Error('작업 이름은 1–160자여야 합니다.');
  roi.name = `${spec.name} · 고정 모델 ROI`;
  roi.lens_revision_id = String(input.lensId || '').trim().toLowerCase();
  roi.sensor_revision.identifier = String(input.sensorId || '').trim();
  roi.sensor_revision.run_id = String(input.sensorRun || '').trim();
  roi.active_mode_declaration_id = String(input.modeId || '').trim();
  if (!/^[0-9a-f-]{36}$/.test(roi.lens_revision_id) || !/^[A-Za-z0-9-]{1,64}$/.test(roi.sensor_revision.identifier) || !/^[0-9a-f]{64}$/.test(roi.sensor_revision.run_id) || !/^[A-Za-z0-9_-]{1,128}$/.test(roi.active_mode_declaration_id)) {
    throw new Error('Lens UUID, Sensor identifier/run SHA와 active-mode ID 형식을 확인하세요.');
  }
  const r0 = frequencyTargetNumber(input.roiR0, 'ROI 행 시작', 0, 65536, true);
  const r1 = frequencyTargetNumber(input.roiR1, 'ROI 행 끝', 1, 65536, true);
  const c0 = frequencyTargetNumber(input.roiC0, 'ROI 열 시작', 0, 65536, true);
  const c1 = frequencyTargetNumber(input.roiC1, 'ROI 열 끝', 1, 65536, true);
  if (r1 - r0 < 8 || r1 - r0 > 128 || c1 - c0 < 8 || c1 - c0 > 128 || (r1 - r0) * (c1 - c0) > 16384) {
    throw new Error('전역 ROI는 양축 8–128 픽셀, 최대 16,384 픽셀이어야 합니다.');
  }
  roi.sensor_roi_bounds_row_col_exclusive = [r0, r1, c0, c1];
  roi.analytic_scene.horizontal_fov_deg = frequencyTargetNumber(input.sceneFov, '장면 수평 화각', 0.001, 90);
  roi.analytic_scene.edge_offset_deg = frequencyTargetNumber(input.edgeOffset, 'Edge 오프셋', -45, 45);
  if (Math.abs(roi.analytic_scene.edge_offset_deg) > roi.analytic_scene.horizontal_fov_deg / 2) throw new Error('Edge 오프셋은 장면 반화각 안에 있어야 합니다.');
  roi.analytic_scene.bright_luminance_cd_m2 = frequencyTargetNumber(input.sceneLuminance, '밝은 쪽 휘도', 0.01, 10000);
  roi.capture.exposure_s = frequencyTargetNumber(input.exposureMs, '노출 시간', 0.001, 1000) / 1000;
  roi.capture.noise_flag = 0;
  roi.sensor_tile_shape_row_col = [frequencyTargetNumber(input.sensorTileRows, '센서 tile 행', 2, 64, true), frequencyTargetNumber(input.sensorTileCols, '센서 tile 열', 2, 64, true)];
  if (roi.sensor_tile_shape_row_col[0] > r1 - r0 || roi.sensor_tile_shape_row_col[1] > c1 - c0) throw new Error('센서 tile은 ROI보다 클 수 없습니다.');
  roi.source_tile_shape_row_col = [frequencyTargetNumber(input.sourceTileRows, '광학 tile 행', 16, 256, true), frequencyTargetNumber(input.sourceTileCols, '광학 tile 열', 16, 256, true)];
  roi.readout_row_chunk_rows = frequencyTargetNumber(input.readoutChunk, 'Readout 행 chunk', 1, 256, true);
  if (roi.readout_row_chunk_rows >= r1 - r0) throw new Error('Readout 행 chunk는 ROI 행 수보다 작아야 합니다.');
  if (input.samplingMode !== 'adaptive' && input.samplingMode !== 'explicit') throw new Error('Adaptive 또는 명시적 격자를 선택하세요.');
  goal.mode = input.samplingMode;
  if (goal.mode === 'explicit') {
    const rows = frequencyTargetNumber(input.sceneRows, 'Scene 시작 행', 32, 65536, true);
    const cols = frequencyTargetNumber(input.sceneCols, 'Scene 시작 열', 32, 65536, true);
    if (rows * cols > 536870912) throw new Error('시작 Scene이 가상 격자 셀 정책을 초과합니다.');
    roi.analytic_scene.shape_row_col = [rows, cols];
  }
  goal.max_refinements = frequencyTargetNumber(input.refinements, '정련 횟수', 0, 4, true);
  if (goal.mode === 'adaptive' && goal.max_refinements < 2 || goal.mode === 'explicit' && goal.max_refinements === 1) {
    throw new Error('Adaptive는 정련 2–4회, 명시적은 단일 격자 또는 정련 2–4회가 필요합니다.');
  }
  goal.target_fraction_native_nyquist = frequencyTargetNumber(input.targetFraction, '목표 Nyquist 비율', 0.05, 1);
  goal.minimum_scene_samples_per_target_period_seed = frequencyTargetNumber(input.samplesPerPeriod, '주기당 Scene 표본 시드', 2, 16, true);
  const fractions = String(input.evaluationFractions || '').split(',').map(value => value.trim());
  if (fractions.length < 2 || fractions.length > 16) throw new Error('평가 주파수는 2–16개가 필요합니다.');
  goal.evaluation_fractions_native_nyquist = fractions.map(value => frequencyTargetNumber(value, '평가 Nyquist 비율', 0.000001, 1));
  if (goal.evaluation_fractions_native_nyquist.some((value, index) => value >= 1 || value > goal.target_fraction_native_nyquist || index > 0 && value <= goal.evaluation_fractions_native_nyquist[index - 1])) {
    throw new Error('평가 주파수는 0과 1 사이 오름차순이고 목표 비율 이하여야 합니다.');
  }
  goal.mean_electron_full_scale_normalized_max_delta_tolerance = frequencyTargetNumber(input.meanTolerance, '평균 광전자 전체 신호 스케일 정규화 최대 변화 허용값', 0.000001, 0.1);
  goal.shift_response_full_scale_normalized_max_delta_tolerance = frequencyTargetNumber(input.shiftTolerance, 'subpixel 응답 전체 신호 스케일 정규화 최대 변화 허용값', 0.000001, 0.1);
  goal.mtf_absolute_tolerance = frequencyTargetNumber(input.mtfTolerance, 'MTF 허용 오차', 0.000001, 0.1);
  goal.fixed_shift_pixel_fraction = frequencyTargetNumber(input.shiftFraction, 'Edge 이동 비율', 0.01, 0.5);
  goal.minimum_normalized_signal_contrast = frequencyTargetNumber(input.contrastFloor, 'Edge 대비 하한', 0.0001, 0.5);
  goal.optical_tile_estimated_working_budget_bytes = frequencyTargetNumber(input.tileBudgetMiB, '광학 tile 추정 예산', 256, 8192, true) * 1048576;
  if (input.psfMode !== 'stored_geometric_only' && input.psfMode !== 'pinned_same_ray_retrace_diagnostic') throw new Error('지원하는 PSF 진단 경로를 선택하세요.');
  goal.psf_resolution_mode = input.psfMode;
  goal.psf_target_pitch_um = input.psfMode === 'stored_geometric_only' || String(input.psfPitch || '').trim() === '' ? null : frequencyTargetNumber(input.psfPitch, 'PSF bin 간격', 0.01, 100);
  if (goal.psf_resolution_mode !== 'stored_geometric_only' && goal.mode === 'explicit' && goal.psf_target_pitch_um === null) throw new Error('명시적 격자에서 PSF 재추적 진단은 PSF bin 간격이 필요합니다.');
  const alias = String(input.aliasGridSeed || '').trim();
  goal.alias_probe_grid_seed_multiple_native_nyquist = alias === '' ? null : frequencyTargetNumber(alias, 'Alias probe 계획 배율', 1.01, 8);
  if (input.ack !== true) throw new Error('모델·실측 경계를 확인해야 합니다.');
  roi.accept_approximate_lens_radiometry = true;
  roi.acknowledge_unverified_mode_and_uniform_sensor_response = true;
  return spec;
}

function frequencyTargetResultFacts(job) {
  if (!job || job.job_kind !== FREQUENCY_TARGET_KIND) throw new Error('선택한 기록은 목표 주파수 표본화 작업이 아닙니다.');
  const summary = job.status === 'succeeded' ? job.result?.summary : null;
  if (job.status === 'succeeded' && (!summary || summary.schema !== FREQUENCY_TARGET_SCHEMA)) throw new Error('저장 결과의 스키마가 제출 계약과 다릅니다.');
  if (!summary) return {summary: null, numeric: false, physical: false, status: job.status};
  const execution = summary.execution || {};
  const psf = summary.psf_target_band_diagnostic || {};
  const physical = summary.physical_accuracy_qualified === true || summary.physical_mtf_comparison_allowed === true || summary.product_ranking_allowed === true;
  if (physical) throw new Error('이 작업의 수치 진단에 물리·제품 승인 주장이 섞여 있습니다.');
  return {
    summary,
    execution,
    psf,
    numeric: String(summary.status || '').startsWith('numeric_'),
    executionNumericallyConverged: String(execution.status || '').startsWith('numeric_'),
    physical: false,
    status: summary.status,
  };
}

function frequencyTargetRequestInit(payload, token) {
  if (payload === undefined) return {method: 'GET'};
  if (typeof token !== 'string' || !token) throw new Error('현재 Camera Platform 세션 토큰이 없습니다. 화면을 새로고침하세요.');
  return {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Platform-Token': token},
    body: JSON.stringify(payload),
  };
}

if (typeof module !== 'undefined' && module.exports) module.exports = {frequencyTargetBuildSpecification, frequencyTargetResultFacts, frequencyTargetRequestInit};

if (typeof document !== 'undefined') {
  (async function frequencyTargetPage() {
    const $ = id => document.getElementById(id);
    const base = '/api/v1/lens-sensor-frequency-target';
    let token = '';
    let template = null;
    let active = null;
    let pollTimer = null;
    let epoch = 0;
    const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
    const finite = value => typeof value === 'number' && Number.isFinite(value) ? Number(value.toPrecision(6)).toString() : '—';
    function error(message) { $('error').textContent = String(message); $('error').hidden = false; }
    function clearError() { $('error').textContent = ''; $('error').hidden = true; }
    async function api(url, payload) {
      const response = await fetch(url, frequencyTargetRequestInit(payload, token));
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = data.error || data.detail;
        throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `서버 응답 ${response.status}`);
      }
      return data;
    }
    const value = id => $(id).value;
    function input() {
      return {
        name:value('study-name'), lensId:value('lens-id'), sensorId:value('sensor-id'), sensorRun:value('sensor-run'), modeId:value('mode-id'),
        roiR0:value('roi-r0'), roiR1:value('roi-r1'), roiC0:value('roi-c0'), roiC1:value('roi-c1'),
        sceneFov:value('scene-fov'), edgeOffset:value('edge-offset'), sceneLuminance:value('scene-luminance'), exposureMs:value('exposure'),
        samplingMode:value('sampling-mode'), refinements:value('refinements'), sceneRows:value('scene-rows'), sceneCols:value('scene-cols'),
        targetFraction:value('target-fraction'), samplesPerPeriod:value('samples-per-period'), evaluationFractions:value('evaluation-fractions'),
        meanTolerance:value('mean-tol'), shiftTolerance:value('shift-tol'), mtfTolerance:value('mtf-tol'), shiftFraction:value('shift-fraction'), contrastFloor:value('contrast-floor'), tileBudgetMiB:value('tile-budget'),
        psfMode:value('psf-mode'), psfPitch:value('psf-pitch'), aliasGridSeed:value('alias-probe'),
        sensorTileRows:value('sensor-tile-rows'), sensorTileCols:value('sensor-tile-cols'), sourceTileRows:value('source-tile-rows'), sourceTileCols:value('source-tile-cols'), readoutChunk:value('readout-chunk'), ack:$('ack').checked,
      };
    }
    function setValue(id, value) { $(id).value = value == null ? '' : String(value); }
    function applyTemplate(spec) {
      template = spec;
      const roi = spec.registered_roi, goal = spec.sampling_goal;
      setValue('study-name', spec.name); setValue('roi-r0', roi.sensor_roi_bounds_row_col_exclusive[0]); setValue('roi-r1', roi.sensor_roi_bounds_row_col_exclusive[1]); setValue('roi-c0', roi.sensor_roi_bounds_row_col_exclusive[2]); setValue('roi-c1', roi.sensor_roi_bounds_row_col_exclusive[3]);
      setValue('scene-fov', roi.analytic_scene.horizontal_fov_deg); setValue('edge-offset', roi.analytic_scene.edge_offset_deg); setValue('scene-luminance', roi.analytic_scene.bright_luminance_cd_m2); setValue('exposure', roi.capture.exposure_s * 1000);
      setValue('sampling-mode', goal.mode); setValue('refinements', goal.max_refinements); setValue('scene-rows', roi.analytic_scene.shape_row_col[0]); setValue('scene-cols', roi.analytic_scene.shape_row_col[1]);
      setValue('target-fraction', goal.target_fraction_native_nyquist); setValue('samples-per-period', goal.minimum_scene_samples_per_target_period_seed); setValue('evaluation-fractions', goal.evaluation_fractions_native_nyquist.join(', '));
      setValue('mean-tol', goal.mean_electron_full_scale_normalized_max_delta_tolerance); setValue('shift-tol', goal.shift_response_full_scale_normalized_max_delta_tolerance); setValue('mtf-tol', goal.mtf_absolute_tolerance); setValue('shift-fraction', goal.fixed_shift_pixel_fraction); setValue('contrast-floor', goal.minimum_normalized_signal_contrast); setValue('tile-budget', goal.optical_tile_estimated_working_budget_bytes / 1048576);
      setValue('psf-mode', goal.psf_resolution_mode); setValue('psf-pitch', goal.psf_target_pitch_um); setValue('alias-probe', goal.alias_probe_grid_seed_multiple_native_nyquist);
      setValue('sensor-tile-rows', roi.sensor_tile_shape_row_col[0]); setValue('sensor-tile-cols', roi.sensor_tile_shape_row_col[1]); setValue('source-tile-rows', roi.source_tile_shape_row_col[0]); setValue('source-tile-cols', roi.source_tile_shape_row_col[1]); setValue('readout-chunk', roi.readout_row_chunk_rows);
      syncMode();
      $('submit').disabled = false;
    }
    function syncMode() {
      const explicit = value('sampling-mode') === 'explicit';
      $('explicit-grid').hidden = !explicit;
      $('scene-rows').required = explicit;
      $('scene-cols').required = explicit;
      const options = $('refinements');
      let zero = options.querySelector('option[value="0"]');
      if (explicit && !zero) { zero = document.createElement('option'); zero.value = '0'; zero.textContent = '0회 · 단일 격자, 수렴 판정 없음'; options.prepend(zero); }
      if (!explicit && zero) { if (options.value === '0') options.value = '2'; zero.remove(); }
      $('mode-note').textContent = explicit ? '명시적 시작 Scene shape를 유지한 채 고정 화각에서 축별 2× 정련합니다. 0회는 한 번만 계산하며 수렴을 판정하지 않습니다.' : 'Adaptive에서는 아래 shape를 실행 격자로 사용하지 않습니다. 센서 pitch, 목표 주파수, 고정 화각과 정책 상한에서 시작 격자를 계획합니다.';
      const retrace = value('psf-mode') === 'pinned_same_ray_retrace_diagnostic';
      $('psf-pitch-box').hidden = !retrace;
      $('psf-pitch').required = retrace && explicit;
    }
    function fact(label, value) { return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`; }
    function renderResult(job) {
      const facts = frequencyTargetResultFacts(job);
      $('result').hidden = false;
      $('permalink').href = `?id=${encodeURIComponent(job.id)}`;
      $('export').href = `${base}/${encodeURIComponent(job.id)}/export`;
      $('export').hidden = job.status !== 'succeeded';
      $('cancel').hidden = !['queued', 'running'].includes(job.status);
      $('result-limits').textContent = facts.summary?.limitation || job.limitation || '수치 모델과 저장 원본의 범위를 확인하세요.';
      $('provenance').textContent = JSON.stringify(facts.summary || {specification:job.specification, registered_roi_submission:job.registered_roi_submission, error:job.error}, null, 2);
      $('artifacts').innerHTML = (job.artifacts || job.result?.artifacts || []).filter(item => item && /^\/api\/v1\/artifacts\/[A-Za-z0-9._/-]+$/.test(item.url || '')).map(item => `<a href="${escapeHtml(item.url)}" download>${escapeHtml(item.name || 'artifact')}</a>`).join('');
      if (!facts.summary) {
        $('result-summary').innerHTML = `<div class="result-state inconclusive"><strong>${escapeHtml(job.status)}</strong><small>${escapeHtml(job.error || 'Worker의 실행·검증 결과를 기다리는 중입니다.')}</small></div>`;
        return;
      }
      const summary = facts.summary, execution = facts.execution, psf = facts.psf;
      const steps = Array.isArray(execution.steps) ? execution.steps : [];
      const stepRows = steps.map(step => `<tr><td>${escapeHtml(step.refinement)}</td><td>${escapeHtml((step.scene_shape_row_col || []).join(' × '))}</td><td>${escapeHtml(finite(step.oi_pitch_um))}</td><td>${escapeHtml(finite(step.executed_psf_convolution_pitch_um))}</td><td>${escapeHtml(step.mean_phase_tolerance_met === true ? '통과' : step.mean_phase_tolerance_met === false ? '미통과' : '미평가')}</td><td>${escapeHtml(step.frequency_target_points_tolerance_met === true ? '통과' : step.frequency_target_points_tolerance_met === false ? '미통과' : '미평가')}</td></tr>`).join('');
      const numericLabel = facts.numeric ? '선언한 수치 모델 안에서만 수렴' : facts.executionNumericallyConverged ? 'Scene/OI 수치 수렴 · 독립 PSF source 해상도 미확립' : '미수렴 또는 판정 불가';
      const plan = summary.sampling_plan || {};
      const nativeNyquist = execution.native_nyquist_cycles_per_mm_column ?? plan.native_nyquist_cycles_per_mm_row_col?.[1];
      const storedPsfPitch = Array.isArray(plan.stored_psf_pitch_um_row_col) ? plan.stored_psf_pitch_um_row_col.map(finite).join(' × ') : '미기록';
      const targetFractions = summary.specification?.sampling_goal?.evaluation_fractions_native_nyquist;
      $('result-summary').innerHTML = `<div class="result-state ${facts.numeric ? '' : 'inconclusive'}"><strong>${escapeHtml(numericLabel)}</strong><small>작업 상태 ${escapeHtml(job.status)} · 세부 상태 ${escapeHtml(summary.status)} · 물리 정확도·제품 순위 미인증</small></div><dl class="summary-grid">${fact('평균 광전자·subpixel 이동', execution.mean_phase_status || '미평가')}${fact('선언한 주파수 점', execution.frequency_target_points_status || '미평가')}${fact('PSF source 해상도', psf.status || execution.psf_source_status || '미평가')}${fact('PSF 재추적 고정 격자 비교', psf.fixed_grid_camera_sensitivity?.status || '미실행')}${fact('센서 열 native Nyquist (cycles/mm)', finite(nativeNyquist))}${fact('평가한 native Nyquist 비율', Array.isArray(targetFractions) ? targetFractions.join(', ') : '미기록')}${fact('저장 원본 기하 PSF 간격 (행 × 열 μm)', storedPsfPitch)}${fact('새 PSF 생성 간격 탐색 시드 (μm) · 실행 증거 아님', finite(plan.psf_generation_pitch_search_seed_um))}${fact('마지막 계산 Scene 격자', (execution.last_executed_scene_shape_row_col || []).join(' × ') || '없음')}${fact('수치 실행 상태', execution.status || '미평가')}${fact('실물 성능 승인', '불가')}</dl><div class="sampling-steps"><table><thead><tr><th>정련</th><th>Scene 행 × 열</th><th>OI 간격 μm</th><th>실행 PSF convolution 간격 μm</th><th>평균·위상</th><th>선언한 주파수 점</th></tr></thead><tbody>${stepRows || '<tr><td colspan="6">실행 가능한 격자가 없었습니다.</td></tr>'}</tbody></table></div><p class="note">간격과 수렴 상태는 현재 등록한 모델·ROI·선언한 이산 주파수 점에 대한 값입니다. 계획상의 alias probe는 실제 above-Nyquist 응답 측정이 아닙니다.</p>`;
    }
    async function listing() {
      const rows = await api(base);
      if (!Array.isArray(rows)) throw new Error('작업 목록 응답이 올바르지 않습니다.');
      $('recent').innerHTML = rows.slice(0, 12).filter(row => row && /^[A-Za-z0-9_-]+$/.test(row.id || '')).map(row => `<button type="button" data-job="${escapeHtml(row.id)}">${escapeHtml(row.name || row.id)} · ${escapeHtml(row.status)}</button>`).join('');
    }
    async function load(id) {
      if (!/^[A-Za-z0-9_-]+$/.test(id)) throw new Error('작업 ID 형식이 올바르지 않습니다.');
      active = id;
      const mine = ++epoch;
      if (pollTimer) clearTimeout(pollTimer);
      const job = await api(`${base}/${encodeURIComponent(id)}`);
      if (mine !== epoch) return;
      if (job.id !== id) throw new Error('선택한 작업 ID와 저장 응답이 다릅니다.');
      $('open-id').value = id;
      $('status').textContent = `${job.name || id} · ${job.status}${job.error ? ' · ' + job.error : ''}`;
      renderResult(job);
      history.replaceState(null, '', `?id=${encodeURIComponent(id)}`);
      if (['queued', 'running'].includes(job.status)) pollTimer = setTimeout(() => load(id).catch(error), 1200);
    }
    $('sampling-mode').addEventListener('change', syncMode);
    $('psf-mode').addEventListener('change', syncMode);
    $('study-form').addEventListener('submit', async event => {
      event.preventDefault(); clearError();
      try {
        const spec = frequencyTargetBuildSpecification(template, input());
        $('submit').disabled = true;
        $('status').textContent = '고정 모델과 목표 표본화 계약을 제출합니다…';
        const job = await api(base, spec);
        if (!job.id || job.job_kind !== FREQUENCY_TARGET_KIND) throw new Error('작업 ID 또는 종류가 제출 계약과 다릅니다.');
        await listing(); await load(job.id);
      } catch (cause) { error(cause.message || cause); }
      finally { $('submit').disabled = false; }
    });
    $('open-form').addEventListener('submit', event => { event.preventDefault(); clearError(); load(value('open-id').trim()).catch(cause => error(cause.message || cause)); });
    $('refresh-list').addEventListener('click', () => listing().catch(cause => error(cause.message || cause)));
    $('recent').addEventListener('click', event => { const item = event.target.closest('[data-job]'); if (item) load(item.dataset.job).catch(cause => error(cause.message || cause)); });
    $('cancel').addEventListener('click', async () => { if (!active) return; try { await api(`${base}/${encodeURIComponent(active)}/cancel`, {}); await load(active); } catch (cause) { error(cause.message || cause); } });
    try {
      const session = await api('/api/v1/session');
      if (session?.schema !== 'camera_platform_v1' || typeof session.token !== 'string' || !session.token) throw new Error('Camera Platform 세션을 확인할 수 없습니다.');
      token = session.token;
      const response = await api(`${base}/template`);
      if (response?.specification?.schema !== FREQUENCY_TARGET_SCHEMA) throw new Error('서버의 목표 주파수 작업 계약이 화면과 다릅니다.');
      applyTemplate(response.specification);
      $('connection').textContent = '서버 template 연결됨 · 등록 모델 ID를 입력하세요.';
      await listing();
      const id = new URLSearchParams(location.search).get('id');
      if (id) await load(id);
    } catch (cause) {
      $('connection').textContent = '목표 주파수 작업 API를 사용할 수 없습니다.';
      error(cause.message || cause);
    }
  })();
}

'use strict';

const DB_COMPARISON_SCHEMA = 'camera_db_comparison_v1';
const READOUT_INPUTS = {
  gain: 'gain', full_well_e: 'full-well', read_noise_e: 'read-noise',
  dark_current_e_per_s: 'dark-current', bit_depth: 'bit-depth',
  black_level_dn: 'black-level',
};

function CameraDbComparisonSpecification(template, lensCatalog, sensorCatalog, input) {
  if (!template || template.schema !== DB_COMPARISON_SCHEMA) {
    throw new Error('현재 화면이 지원하지 않는 DB 비교 입력 계약입니다.');
  }
  const lenses = lensCatalog?.revisions;
  if (!Array.isArray(lenses) || !Array.isArray(sensorCatalog)) {
    throw new Error('렌즈 또는 센서 DB 목록이 없습니다.');
  }
  const lensIds = [input.lensA, input.lensB];
  if (lensIds.some(id => !id) || new Set(lensIds).size !== 2 ||
      lensIds.some(id => !lenses.some(row => row.revision_id === id && row.preview_status === 'ready'))) {
    throw new Error('실행 가능한 서로 다른 렌즈 revision 2개를 선택하세요.');
  }
  const sensorRuns = [input.sensorA, input.sensorB];
  if (sensorRuns.some(id => !id) || new Set(sensorRuns).size !== 2) {
    throw new Error('저장 실행이 서로 다른 센서 revision 2개를 선택하세요.');
  }
  const selectedSensors = sensorRuns.map(runId => sensorCatalog.find(row =>
    row.run_id === runId && row.status === 'simulated' && (row.report_code || row.sensor_id)));
  if (selectedSensors.some(row => !row)) {
    throw new Error('선택한 센서 중 실행 가능한 저장 revision이 없습니다.');
  }
  const name = String(input.name || '').trim();
  if (!name || name.length > 160) throw new Error('비교 이름은 1–160자로 입력하세요.');
  const wavelength = Number(input.wavelength);
  const exposureMs = Number(input.exposureMs);
  const height = Number(input.height), width = Number(input.width);
  const noise = Number(input.noise), seed = Number(input.seed);
  if (!Number.isFinite(wavelength) || wavelength <= 0) throw new Error('유효한 저장 PSF 파장을 입력하세요.');
  if (!Number.isFinite(exposureMs) || exposureMs <= 0 || exposureMs > 1000) throw new Error('노출은 0–1000 ms 사이여야 합니다.');
  if (![height, width].every(v => Number.isInteger(v) && v >= 8 && v <= 32)) throw new Error('장면 크기는 각 축 8–32 픽셀이어야 합니다.');
  if (![0, 2].includes(noise)) throw new Error('센서 잡음 모드를 선택하세요.');
  if (!Number.isInteger(seed) || seed < 0 || seed >= 2 ** 32) throw new Error('seed는 uint32 정수여야 합니다.');
  if (input.acknowledge !== true) throw new Error('시뮬레이션 지원 범위를 확인하세요.');
  const overrides = {};
  for (const key of Object.keys(READOUT_INPUTS)) {
    const entered = input.readoutOverrides?.[key];
    if (entered === undefined || entered === null || String(entered).trim() === '') continue;
    const value = Number(entered);
    if (!Number.isFinite(value) || value < 0 ||
        (['bit_depth', 'black_level_dn'].includes(key) && !Number.isInteger(value))) {
      throw new Error(`${key}에 유효한 공통 readout 값을 입력하세요.`);
    }
    overrides[key] = value;
  }
  return {
    ...template,
    name,
    lens_revision_ids: lensIds,
    sensor_revisions: selectedSensors.map(row => ({
      identifier: row.report_code || row.sensor_id,
      run_id: row.run_id,
    })),
    wavelength_nm: wavelength,
    scene: {type: 'uniform ee', args: [[height, width]]},
    sensor_size: [height, width],
    exposure_s: exposureMs / 1000,
    noise_flag: noise,
    seed,
    readout_overrides: overrides,
    acknowledge_simulation_limits: true,
  };
}

function CameraDbComparisonTracks(job) {
  if (job?.status !== 'succeeded') return null;
  const summary = job.result?.summary;
  if (summary?.schema !== DB_COMPARISON_SCHEMA || summary.status !== 'completed' ||
      !Array.isArray(summary.lens_geometry?.lenses) || summary.lens_geometry.lenses.length !== 2 ||
      !Array.isArray(summary.sensor_raw?.captures) || summary.sensor_raw.captures.length !== 2 ||
      summary.lens_sensor_cross_product?.status !== 'not_executed') {
    throw new Error('저장된 DB 비교 결과가 두 개의 별도 트랙 계약과 일치하지 않습니다.');
  }
  return summary;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {CameraDbComparisonSpecification, CameraDbComparisonTracks};
}

if (typeof document !== 'undefined') (() => {
  const $ = id => document.getElementById(id);
  const make = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = String(value);
    return element;
  };
  const fmt = (value, digits = 3) => typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString('ko-KR', {maximumFractionDigits: digits}) : '—';
  const value = id => $(id).value;
  let token = '', template = null, lensCatalog = null, sensorCatalog = null;
  let currentId = '', timer = null, epoch = 0, submitting = false;

  async function api(path, payload) {
    const response = await fetch('/api/v1/' + path, {
      method: payload === undefined ? 'GET' : 'POST',
      headers: payload === undefined ? {} : {'Content-Type': 'application/json', 'X-Platform-Token': token},
      ...(payload === undefined ? {} : {body: JSON.stringify(payload)}),
    });
    const raw = await response.text();
    let data;
    try { data = JSON.parse(raw); } catch { data = null; }
    if (!response.ok) throw new Error(data?.error || data?.detail || `HTTP ${response.status}`);
    if (!data || typeof data !== 'object') throw new Error('서버 응답이 JSON 객체가 아닙니다.');
    return data;
  }
  function showError(error) {
    $('error').hidden = false;
    $('error').textContent = error?.message || String(error);
  }
  function clearError() { $('error').hidden = true; $('error').textContent = ''; }
  function lensLabel(row) {
    return [row.manufacturer, row.name, row.configuration, row.revision_id].filter(Boolean).join(' · ');
  }
  function sensorLabel(row) {
    return [row.manufacturer, row.device_name || row.model, row.report_code || row.sensor_id, row.run_id].filter(Boolean).join(' · ');
  }
  function fillSelect(id, rows, idOf, labelOf, eligible, query, preferred) {
    const select = $(id), selected = select.value || preferred || '';
    const shown = rows.filter(row => !query || labelOf(row).toLocaleLowerCase().includes(query.toLocaleLowerCase()) || idOf(row) === selected);
    const choices = [make('option', '', '선택하세요')];
    choices[0].value = '';
    for (const row of shown) {
      const option = make('option', '', labelOf(row) + (eligible(row) ? '' : ' · 실행 불가'));
      option.value = idOf(row);
      option.disabled = !eligible(row);
      if (!eligible(row)) option.title = row.reason_code || row.reason || '지원되지 않는 revision';
      choices.push(option);
    }
    select.replaceChildren(...choices);
    select.value = shown.some(row => idOf(row) === selected) ? selected : '';
  }
  function refreshCatalogOptions() {
    if (!template) return;
    const lenses = lensCatalog.revisions, sensors = sensorCatalog;
    const readyLenses = lenses.filter(row => row.preview_status === 'ready');
    const readySensors = sensors.filter(row => row.status === 'simulated' && row.run_id);
    fillSelect('lens-a', lenses, row => row.revision_id, lensLabel, row => row.preview_status === 'ready',
      value('lens-search'), readyLenses[0]?.revision_id);
    fillSelect('lens-b', lenses, row => row.revision_id, lensLabel, row => row.preview_status === 'ready',
      value('lens-search'), readyLenses[1]?.revision_id);
    fillSelect('sensor-a', sensors, row => row.run_id || '', sensorLabel,
      row => row.status === 'simulated' && !!row.run_id, value('sensor-search'), readySensors[0]?.run_id);
    fillSelect('sensor-b', sensors, row => row.run_id || '', sensorLabel,
      row => row.status === 'simulated' && !!row.run_id, value('sensor-search'), readySensors[1]?.run_id);
    $('lens-count').textContent = `전체 ${lensCatalog.total ?? lenses.length}개 revision · geometric 미리보기 가능 ${lensCatalog.preview_ready_count ?? readyLenses.length}개`;
    $('sensor-count').textContent = `전체 ${sensors.length}개 저장 revision · 실행 가능 ${readySensors.length}개`;
    updateSubmit();
  }
  function formInput() {
    const readoutOverrides = {};
    for (const [key, id] of Object.entries(READOUT_INPUTS)) readoutOverrides[key] = value(id);
    return {
      name: value('name'), lensA: value('lens-a'), lensB: value('lens-b'),
      sensorA: value('sensor-a'), sensorB: value('sensor-b'),
      wavelength: value('wavelength'), exposureMs: value('exposure'),
      height: value('height'), width: value('width'), noise: value('noise'), seed: value('seed'),
      readoutOverrides, acknowledge: $('acknowledge').checked,
    };
  }
  function updateSubmit() {
    $('submit').disabled = !template || !token || submitting;
    if (!template) return;
    try {
      CameraDbComparisonSpecification(template, lensCatalog, sensorCatalog, formInput());
      $('selection-status').textContent = '두 렌즈와 두 센서의 선택이 준비됐습니다.';
    } catch (error) {
      $('submit').disabled = true;
      $('selection-status').textContent = error.message;
    }
  }
  function table(headers, rows) {
    const wrap = make('div', 'table-wrap'), body = make('table', 'comparison-table');
    const head = make('thead'), headerRow = make('tr');
    for (const label of headers) headerRow.append(make('th', '', label));
    head.append(headerRow); body.append(head);
    const tbody = make('tbody');
    for (const cells of rows) {
      const row = make('tr');
      for (const cell of cells) row.append(make('td', '', cell));
      tbody.append(row);
    }
    body.append(tbody); wrap.append(body); return wrap;
  }
  function renderLens(summary) {
    const host = $('lens-results'); host.replaceChildren();
    const lenses = summary.lens_geometry.lenses;
    const rows = lenses.map(item => {
      const center = item.fields.find(field => field.position === 'center') || {};
      const edge = item.fields.find(field => field.position === 'edge') || {};
      return [
        item.identity?.revision_id || '—',
        `${fmt(center.ee80_radius_um)} / ${fmt(edge.ee80_radius_um)}`,
        `${fmt(center.rms_radius_um)} / ${fmt(edge.rms_radius_um)}`,
        `${fmt(center.geometric_chief_ray_angle_deg)} / ${fmt(edge.geometric_chief_ray_angle_deg)}`,
        `${fmt(center.retained_energy_fraction, 4)} / ${fmt(edge.retained_energy_fraction, 4)}`,
      ];
    });
    host.append(table(['렌즈 revision', 'EE80 반경 중심 / 주변 (µm)', 'RMS 반경 중심 / 주변 (µm)',
      'CRA 중심 / 주변 (°)', '저장 ray 비율 중심 / 주변'], rows));
    const cards = make('div', 'cards');
    for (const item of lenses) {
      const card = make('article', 'card');
      card.append(make('h4', '', item.identity?.publication_number || item.identity?.revision_id || '렌즈'));
      card.append(make('p', '', `revision ${item.identity?.revision_id || '—'} · ${item.identity?.configuration || '구성 미기록'}`));
      const dl = make('dl');
      for (const [label, val] of [
        ['유효 초점거리', `${fmt(item.model?.effective_focal_length_mm)} mm`],
        ['유효 F 수', fmt(item.model?.effective_f_number)],
        ['물체 거리', item.model?.object_distance_m ?? '—'],
        ['회절 포함', item.model?.diffraction_included === false ? '아니오' : '미확인'],
        ['절대 투과율', item.model?.absolute_transmission || '미확인'],
      ]) { dl.append(make('dt', '', label), make('dd', '', val)); }
      card.append(dl); cards.append(card);
    }
    host.append(cards);
  }
  function rawPreview(raw) {
    const values = raw?.values;
    if (!Array.isArray(values) || !Array.isArray(values[0])) return make('p', 'note', 'RAW 값 미리보기가 없습니다.');
    const rows = Math.min(values.length, 8), cols = Math.min(values[0].length, 8);
    const min = Number(raw.min_dn), max = Number(raw.max_dn), span = max > min ? max - min : 1;
    const grid = make('div', 'raw-grid'); grid.style.setProperty('--columns', String(cols));
    grid.setAttribute('aria-label', `RAW 왼쪽 위 ${rows}×${cols} 코드 미리보기`);
    for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
      const code = Number(values[r]?.[c]);
      const t = Number.isFinite(code) ? Math.min(1, Math.max(0, (code - min) / span)) : 0;
      const shade = Math.round(232 - t * 205);
      const cell = make('span'); cell.style.backgroundColor = `rgb(${shade},${shade},${shade})`;
      cell.title = `${r}, ${c}: ${fmt(code, 0)} DN`;
      grid.append(cell);
    }
    return grid;
  }
  function renderSensor(summary) {
    const host = $('sensor-results'); host.replaceChildren();
    const cards = make('div', 'cards');
    for (const capture of summary.sensor_raw.captures) {
      const card = make('article', 'card');
      const revision = capture.revision || {};
      card.append(make('h4', '', revision.device_name || revision.report_code || revision.sensor_id || '센서'));
      card.append(make('p', '', `${revision.report_code || revision.sensor_id || '—'} · 저장 run ${revision.run_id || '—'}`));
      const dl = make('dl');
      for (const [label, val] of [
        ['RAW 최솟값', `${fmt(capture.raw?.min_dn, 0)} DN`],
        ['RAW 최댓값', `${fmt(capture.raw?.max_dn, 0)} DN`],
        ['평균 광전자', `${fmt(capture.photoelectrons?.mean)} e⁻/pixel`],
        ['readout 분류', capture.evidence?.readout_classification || '미기록'],
      ]) { dl.append(make('dt', '', label), make('dd', '', val)); }
      card.append(dl, rawPreview(capture.raw), make('p', 'raw-legend', 'RAW 왼쪽 위 최대 8×8 · 각 카드 내부 min/max 밝기 정규화'));
      const artifact = capture.raw_artifact;
      if (artifact && /^\/api\/v1\/artifacts\/[0-9a-f]{64}$/.test(artifact.url || '')) {
        const link = make('a', '', '이 센서의 원본 RAW NPY 저장');
        link.href = artifact.url; link.download = artifact.name || 'sensor-raw.npy';
        card.append(link);
      }
      cards.append(card);
    }
    host.append(cards);
    const captures = summary.sensor_raw.captures;
    const overrides = summary.sensor_raw.conditions?.readout_overrides || {};
    const effective = summary.sensor_raw.conditions?.effective_readout || {};
    const readoutLabels = {
      gain: 'gain', full_well_e: 'full well (e⁻)', read_noise_e: 'read noise (e⁻ RMS)',
      dark_current_e_per_s: 'dark current (e⁻/pixel/s)', bit_depth: 'bit depth',
      black_level_dn: 'black level (DN)',
    };
    const saved = (capture, key) => {
      const entry = capture.capture?.saved_readout?.[key];
      return entry ? `${fmt(entry.value, 4)} · ${entry.classification || '분류 미기록'}` : '미기록';
    };
    host.append(make('h4', '', '저장 readout과 공통 적용값'));
    host.append(table(['필드', '센서 A 저장값 · 분류', '센서 B 저장값 · 분류', '공통 override', '실행값'],
      Object.entries(readoutLabels).map(([key, label]) => [
        label, saved(captures[0], key), saved(captures[1], key),
        Object.hasOwn(overrides, key) ? fmt(overrides[key], 4) : '없음', fmt(effective[key], 4),
      ])));
    const changes = summary.sensor_raw.differences || {};
    const changeRows = [
      ['평균 RAW', changes.mean_raw_dn],
      ['평균 black 기준 RAW', changes.mean_black_relative_raw_dn],
      ['평균 광전자', changes.mean_photoelectrons],
    ].map(([label, item]) => [label, fmt(item?.first), fmt(item?.second), fmt(item?.second_minus_first), item?.unit || '—']);
    host.append(table(['집계값', '센서 A', '센서 B', 'B − A', '단위'], changeRows));
    const source = summary.sensor_raw.source_differences || {};
    const printable = item => item === undefined || item === null ? '—'
      : typeof item === 'object' ? JSON.stringify(item) : String(item);
    const sourceRows = [
      ['모델 종류', source.model_kind],
      ['픽셀 피치 (µm)', source.pixel_pitch_um],
      ['readout 근거 분류', source.readout_profile_classification],
    ].map(([label, item]) => [label, printable(item?.first), printable(item?.second), item?.different ? '다름' : '같음']);
    host.append(make('h4', '', '두 revision의 출처와 가정'));
    host.append(table(['항목', '센서 A', '센서 B', '차이'], sourceRows));
    host.append(make('p', 'note', `공통 readout 변경: ${Object.keys(overrides).length ? JSON.stringify(overrides) : '없음 · 각 DB 저장값 사용'}`));
    host.append(make('p', 'note', '차이는 선언된 조건의 집계값입니다. CFA·픽셀 피치·광학 모델 출처가 다를 수 있으므로 제품 순위로 읽지 마세요.'));
  }
  function render(job) {
    const summary = CameraDbComparisonTracks(job);
    $('result').hidden = !summary;
    if (!summary) return;
    $('result-limits').textContent = summary.limitation || '적용 범위를 확인하세요.';
    renderLens(summary); renderSensor(summary);
    $('cross-product').textContent = `미실행 · ${summary.lens_sensor_cross_product.reason || '검증된 광량·센서 sampling 결합 모델이 없습니다.'}`;
    $('provenance').textContent = JSON.stringify({
      specification: summary.specification,
      source_identities: summary.source_identities,
      verification: summary.verification,
      sensor_source_differences: summary.sensor_raw.source_differences,
      effective_readout: summary.sensor_raw.conditions?.effective_readout,
    }, null, 2);
    $('permalink').href = `?comparison=${encodeURIComponent(job.id)}`;
    $('export').href = `/api/v1/db-comparisons/${encodeURIComponent(job.id)}/export`;
  }
  async function loadJob(id) {
    if (!id) throw new Error('실행 ID를 입력하세요.');
    const mine = ++epoch; currentId = id; clearTimeout(timer); clearError();
    $('job-id').value = id; $('result').hidden = true;
    history.replaceState(null, '', `?comparison=${encodeURIComponent(id)}`);
    const lifecycle = await api('jobs/' + encodeURIComponent(id) + '?expected_kind=db_comparison');
    if (mine !== epoch) return;
    if (lifecycle.id !== id || lifecycle.job_kind !== 'db_comparison') throw new Error('선택한 DB 비교 ID와 작업 상태가 다릅니다.');
    $('status').textContent = `${lifecycle.status} · ${id}${lifecycle.error ? ' · ' + lifecycle.error : ''}`;
    if (lifecycle.status === 'queued' || lifecycle.status === 'running') {
      timer = setTimeout(() => loadJob(id).catch(showError), 1000);
      return;
    }
    const detail = await api('db-comparisons/' + encodeURIComponent(id));
    if (mine !== epoch) return;
    if (detail.id !== id || detail.job_kind !== 'db_comparison') throw new Error('선택한 DB 비교와 상세 기록이 일치하지 않습니다.');
    if (detail.status === 'failed' || detail.status === 'cancelled') {
      $('status').textContent = `${detail.status} · ${detail.error || '이 실행에서는 결과가 생성되지 않았습니다.'}`;
    }
    render(detail);
  }
  async function init() {
    const session = await api('session');
    if (session.schema !== 'camera_platform_v1' || !session.token) throw new Error('Camera Platform 세션이 아닙니다.');
    token = session.token;
    $('connection').textContent = '현재 로컬 작업공간에 연결했습니다.';
    const response = await api('db-comparisons/template');
    template = response.specification; lensCatalog = response.lens_catalog; sensorCatalog = response.sensor_catalog;
    if (template?.schema !== DB_COMPARISON_SCHEMA) throw new Error('서버의 DB 비교 계약 버전이 다릅니다.');
    $('name').value = template.name || '';
    $('wavelength').value = template.wavelength_nm;
    $('exposure').value = Number(template.exposure_s) * 1000;
    $('height').value = template.sensor_size?.[0] ?? 8;
    $('width').value = template.sensor_size?.[1] ?? 8;
    $('noise').value = String(template.noise_flag ?? 0);
    $('seed').value = template.seed ?? 7;
    $('limits').textContent = response.limitation || '이 비교의 지원 범위를 확인하세요.';
    refreshCatalogOptions();
    const id = new URLSearchParams(location.search).get('comparison');
    if (id) await loadJob(id);
  }
  $('comparison-form').addEventListener('input', event => {
    if (event.target.id === 'lens-search' || event.target.id === 'sensor-search') refreshCatalogOptions();
    else updateSubmit();
  });
  $('comparison-form').addEventListener('change', updateSubmit);
  $('comparison-form').addEventListener('submit', async event => {
    event.preventDefault(); if (submitting) return;
    clearError(); submitting = true; updateSubmit(); $('status').textContent = '선택한 revision과 조건을 확인해 제출 중…';
    try {
      const spec = CameraDbComparisonSpecification(template, lensCatalog, sensorCatalog, formInput());
      const queued = await api('db-comparisons', spec);
      if (!queued.id || queued.job_kind !== 'db_comparison') throw new Error('서버가 DB 비교 작업 ID를 반환하지 않았습니다.');
      await loadJob(queued.id);
    } catch (error) { showError(error); $('status').textContent = '제출 또는 조회에 실패했습니다. 선택 조건과 서버 메시지를 확인하세요.'; }
    finally { submitting = false; updateSubmit(); }
  });
  $('open-form').addEventListener('submit', event => {
    event.preventDefault(); loadJob(value('job-id').trim()).catch(showError);
  });
  init().catch(error => { $('connection').textContent = '작업공간 연결 또는 DB 목록을 확인할 수 없습니다.'; showError(error); updateSubmit(); });
})();

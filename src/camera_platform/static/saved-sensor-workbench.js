'use strict';

const SAVED_SENSOR_QUALITY_SCHEMA = 'camerae2e_saved_sensor_quality_study_v1';
const SAVED_SENSOR_SEARCH_SCHEMA = 'camerae2e_saved_sensor_search_study_v1';
const SAVED_SENSOR_SCENES = new Set(['uniform', 'slanted_edge', 'macbeth_d65', 'hdr_chart_d65']);

function sensorWorkbenchNumber(value, name, low, high, integer = false) {
  if (value === undefined || value === null || String(value).trim() === '') {
    throw new Error(`${name} 값을 입력하세요.`);
  }
  const number = Number(value);
  if (!Number.isFinite(number) || number < low || number > high || (integer && !Number.isInteger(number))) {
    throw new Error(`${name} 값은 ${low}–${high}${integer ? ' 정수' : ''}여야 합니다.`);
  }
  return number;
}

function sensorWorkbenchSource(catalog, runId) {
  const row = catalog.find(item => item.run_id === runId);
  if (!row || row.status !== 'simulated' || !(row.report_code || row.sensor_id)) {
    throw new Error('실행 가능한 저장 센서 run을 선택하세요. 선택한 항목의 지원 범위도 확인하세요.');
  }
  return {identifier: row.report_code || row.sensor_id, run_id: row.run_id};
}

function SensorWorkbenchQualitySpecification(template, catalog, input) {
  if (template?.schema !== SAVED_SENSOR_QUALITY_SCHEMA || !Array.isArray(catalog)) {
    throw new Error('화질 진단 입력 계약 또는 저장 센서 목록이 없습니다.');
  }
  const name = String(input.name || '').trim();
  if (!name || name.length > 160) throw new Error('작업 이름은 1–160자로 입력하세요.');
  const ids = [input.sensorA, input.sensorB].filter(Boolean);
  if (!ids.length || ids.length > 2 || new Set(ids).size !== ids.length) {
    throw new Error('서로 다른 저장 센서 run을 한 개 또는 두 개 선택하세요.');
  }
  const scenes = input.scenes;
  if (!Array.isArray(scenes) || scenes.length < 1 || scenes.length > 3 ||
      new Set(scenes).size !== scenes.length || scenes.some(scene => !SAVED_SENSOR_SCENES.has(scene))) {
    throw new Error('서로 다른 지원 장면을 1–3개 선택하세요.');
  }
  const rows = sensorWorkbenchNumber(input.rows, '출력 행', 16, 32, true);
  const cols = sensorWorkbenchNumber(input.cols, '출력 열', 16, 32, true);
  if (scenes.includes('hdr_chart_d65') && Math.min(rows, cols) < 32) {
    throw new Error('HDR chart의 6단 밝기 진단은 32×32 출력이 필요합니다.');
  }
  if (input.acknowledge !== true) throw new Error('시뮬레이션 지원 범위를 확인하세요.');
  return {
    ...template,
    name,
    sensor_revisions: ids.map(id => sensorWorkbenchSource(catalog, id)),
    scenes,
    luminance_cd_m2: sensorWorkbenchNumber(input.luminance, '평균 휘도', .01, 10000),
    exposure_s: sensorWorkbenchNumber(input.exposureMs, '노출', .001, 1000) / 1000,
    frames: sensorWorkbenchNumber(input.frames, '독립 RAW 프레임', 2, 4, true),
    output_size: [rows, cols],
    seed: sensorWorkbenchNumber(input.seed, 'seed', 0, 2 ** 32 - 1, true),
    acknowledge_simulation_limits: true,
  };
}

function sensorWorkbenchAxis(value, name, low, high) {
  const parts = String(value || '').split(',').map(item => item.trim());
  if (parts.length < 1 || parts.length > 4 || parts.some(item => !item)) {
    throw new Error(`${name} 후보를 쉼표로 구분해 1–4개 입력하세요.`);
  }
  const numbers = parts.map(item => sensorWorkbenchNumber(item, name, low, high));
  if (new Set(numbers).size !== numbers.length) throw new Error(`${name} 후보는 서로 달라야 합니다.`);
  return numbers;
}

function SensorWorkbenchSearchSpecification(template, catalog, input) {
  if (template?.schema !== SAVED_SENSOR_SEARCH_SCHEMA || !Array.isArray(catalog)) {
    throw new Error('노출·gain 탐색 입력 계약 또는 저장 센서 목록이 없습니다.');
  }
  const name = String(input.name || '').trim();
  if (!name || name.length > 160) throw new Error('작업 이름은 1–160자로 입력하세요.');
  const source = sensorWorkbenchSource(catalog, input.sensor);
  const exposuresMs = sensorWorkbenchAxis(input.exposuresMs, '노출 (ms)', .001, 1000);
  const gains = sensorWorkbenchAxis(input.gains, 'Analog gain', 1, 16);
  if (exposuresMs.length * gains.length > 4) {
    throw new Error('노출×gain 조합은 최대 4개만 실행할 수 있습니다.');
  }
  if (input.acknowledge !== true) throw new Error('탐색의 모델·후보 제한을 확인하세요.');
  return {
    ...template,
    name,
    sensor_revision: source,
    exposure_s: exposuresMs.map(ms => ms / 1000),
    analog_gain: gains,
    luminance_cd_m2: sensorWorkbenchNumber(input.luminance, '휘도', .01, 10000),
    sensor_size: [
      sensorWorkbenchNumber(input.rows, '계산 행', 8, 32, true),
      sensorWorkbenchNumber(input.cols, '계산 열', 8, 32, true),
    ],
    max_saturation_fraction: sensorWorkbenchNumber(input.ceiling, '최대 포화 비율', 0, 1),
    seed: sensorWorkbenchNumber(input.seed, 'seed', 0, 2 ** 32 - 1, true),
    acknowledge_simulation_limits: true,
  };
}

function SensorWorkbenchSummary(kind, job) {
  if (job?.status !== 'succeeded') return null;
  const summary = job.result?.summary;
  if (kind === 'quality' && (job.job_kind !== 'saved_sensor_quality' ||
      summary?.schema !== SAVED_SENSOR_QUALITY_SCHEMA || summary.status !== 'completed' ||
      !Array.isArray(summary.sensor_reports) || !summary.sensor_reports.length)) {
    throw new Error('저장된 화질 진단 결과의 계약이 현재 화면과 일치하지 않습니다.');
  }
  if (kind === 'search' && (job.job_kind !== 'saved_sensor_search' ||
      summary?.schema !== SAVED_SENSOR_SEARCH_SCHEMA || summary.status !== 'completed' ||
      summary.search?.schema !== 'camerae2e_saved_sensor_search_v1' ||
      !Array.isArray(summary.search.cases) || summary.search.case_count !== summary.search.cases.length ||
      summary.search.case_count > 4)) {
    throw new Error('저장된 탐색 결과의 계약이 현재 화면과 일치하지 않습니다.');
  }
  return summary;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    SensorWorkbenchQualitySpecification, SensorWorkbenchSearchSpecification, SensorWorkbenchSummary,
  };
}

if (typeof document !== 'undefined') (() => {
  const $ = id => document.getElementById(id);
  const make = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = String(value);
    return element;
  };
  const fmt = (number, digits = 3) => typeof number === 'number' && Number.isFinite(number)
    ? number.toLocaleString('ko-KR', {maximumFractionDigits: digits}) : '—';
  const value = id => $(id).value;
  const paths = {quality: 'saved-sensor-quality', search: 'saved-sensor-search'};
  const kinds = {quality: 'saved_sensor_quality', search: 'saved_sensor_search'};
  let token = '', qualityTemplate = null, searchTemplate = null;
  let qualityCatalog = [], searchCatalog = [];
  let active = null, timer = null, epoch = 0, qualitySubmitting = false, searchSubmitting = false;

  async function api(path, payload) {
    const response = await fetch('/api/v1/' + path, payload === undefined ? {} : {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Platform-Token': token},
      body: JSON.stringify(payload),
    });
    const raw = await response.text();
    let data;
    try { data = JSON.parse(raw); } catch { data = null; }
    if (!response.ok) {
      const detail = data?.error || data?.detail;
      throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `HTTP ${response.status}`);
    }
    if (data === null || typeof data !== 'object') throw new Error('서버가 JSON 결과를 반환하지 않았습니다.');
    return data;
  }
  function showError(error) { $('error').hidden = false; $('error').textContent = error?.message || String(error); }
  function clearError() { $('error').hidden = true; $('error').textContent = ''; }
  function sensorLabel(row) {
    return [row.manufacturer, row.device_name || row.model, row.report_code || row.sensor_id, row.run_id]
      .filter(Boolean).join(' · ');
  }
  function fillSelect(id, catalog, optional, preferred) {
    const select = $(id), selected = select.value || preferred || '';
    const filter = value('sensor-filter').toLocaleLowerCase();
    const shown = catalog.filter(row => !filter || sensorLabel(row).toLocaleLowerCase().includes(filter) || row.run_id === selected);
    const options = [make('option', '', optional ? '추가 센서 없음' : '센서 run 선택')];
    options[0].value = '';
    for (const row of shown) {
      const eligible = row.status === 'simulated' && !!row.run_id && !!(row.report_code || row.sensor_id);
      const option = make('option', '', sensorLabel(row) + (eligible ? '' : ' · 실행 불가'));
      option.value = row.run_id || '';
      option.disabled = !eligible;
      if (!eligible) option.title = row.reason || '저장 시뮬레이션이 없습니다.';
      options.push(option);
    }
    select.replaceChildren(...options);
    select.value = shown.some(row => row.run_id === selected) ? selected : '';
  }
  function refreshSensors() {
    fillSelect('quality-sensor-a', qualityCatalog, false);
    fillSelect('quality-sensor-b', qualityCatalog, true);
    fillSelect('search-sensor', searchCatalog, false);
    const ready = qualityCatalog.filter(row => row.status === 'simulated').length;
    $('sensor-count').textContent = `${qualityCatalog.length}개 저장 항목 중 ${ready}개가 simulated 상태입니다. 실제 실행 가능 여부는 제출 시 저장 hash·런타임·광학 계약으로 검사합니다.`;
    updateForms();
  }
  function qualityInput() {
    return {
      name: value('quality-name'), sensorA: value('quality-sensor-a'), sensorB: value('quality-sensor-b'),
      scenes: [...document.querySelectorAll('input[name="quality-scene"]:checked')].map(input => input.value),
      luminance: value('quality-luminance'), exposureMs: value('quality-exposure'),
      frames: value('quality-frames'), rows: value('quality-rows'), cols: value('quality-cols'),
      seed: value('quality-seed'), acknowledge: $('quality-acknowledge').checked,
    };
  }
  function searchInput() {
    return {
      name: value('search-name'), sensor: value('search-sensor'),
      exposuresMs: value('search-exposures'), gains: value('search-gains'),
      luminance: value('search-luminance'), ceiling: value('search-ceiling'),
      rows: value('search-rows'), cols: value('search-cols'), seed: value('search-seed'),
      acknowledge: $('search-acknowledge').checked,
    };
  }
  function updateForms() {
    try {
      SensorWorkbenchQualitySpecification(qualityTemplate, qualityCatalog, qualityInput());
      $('quality-submit').disabled = qualitySubmitting;
    } catch { $('quality-submit').disabled = true; }
    try {
      const spec = SensorWorkbenchSearchSpecification(searchTemplate, searchCatalog, searchInput());
      $('candidate-count').textContent = `${spec.exposure_s.length} 노출 × ${spec.analog_gain.length} gain = ${spec.exposure_s.length * spec.analog_gain.length}개 후보`;
      $('search-submit').disabled = searchSubmitting;
    } catch (error) {
      $('candidate-count').textContent = error.message;
      $('search-submit').disabled = true;
    }
  }
  function table(headers, rows) {
    const wrap = make('div', 'table-wrap'), tableElement = make('table', 'result-table');
    const head = make('thead'), heading = make('tr');
    for (const label of headers) heading.append(make('th', '', label));
    head.append(heading); tableElement.append(head);
    const body = make('tbody');
    for (const cells of rows) {
      const row = make('tr');
      for (const cell of cells) row.append(make('td', '', cell));
      body.append(row);
    }
    tableElement.append(body); wrap.append(tableElement); return wrap;
  }
  function metric(item, unit = '') {
    return item?.available ? `${fmt(item.value)}${item.unit || unit ? ' ' + (item.unit || unit) : ''}`
      : item?.reason || '사용 불가';
  }
  function renderQuality(summary) {
    const host = $('result-summary'); host.replaceChildren();
    host.append(make('h3', '', '장면별 결과'));
    const rows = [];
    for (const report of summary.sensor_reports) {
      const source = report.revision?.report_code || report.revision?.sensor_id || '센서';
      for (const item of report.cases || []) {
        const m = item.metrics || {};
        const saturation = m.saturation_fractions || {};
        rows.push([
          source, item.scene || '—', fmt(m.mean_photoelectrons),
          metric(m.native_raw_snr_db), metric(m.common_output_snr_db),
          metric(m.edge_mtf50), metric(m.macbeth_cie76_mean),
          Object.entries(saturation).map(([key, fraction]) => `${key} ${fmt(fraction, 4)}`).join(' · ') || '—',
        ]);
      }
    }
    host.append(table(['센서', '장면', '광전자 e⁻/pixel', 'RAW 시간 SNR', '출력 시간 SNR',
      '시스템 MTF50', 'Macbeth 보정 잔차', '전체 프레임 포화 비율'], rows));
    host.append(make('p', 'note', 'MTF50은 광학·센서·ISP·crop을 포함합니다. Macbeth 값은 동일 패치를 보정과 평가에 사용한 잔차입니다. 포화 시 SNR은 진단값입니다.'));
    for (const report of summary.sensor_reports) {
      for (const item of report.cases || []) {
        const response = item.metrics?.hdr_chart_response;
        if (!response?.available) continue;
        const card = make('section', 'subresult');
        card.append(make('h4', '', `${report.revision?.report_code || '센서'} · HDR chart 단일 노출 6단 응답`));
        card.append(make('p', 'note', `선언 장면 명암비 ${fmt(response.input_dynamic_range_ratio, 0)}:1 · HDR 합성 ${response.hdr_fusion_performed ? '실행' : '미실행'} · LFM ${response.lfm_evaluated ? '평가' : '미평가'}`));
        const levels = response.levels_bright_to_dark || [];
        card.append(table(['밝기 단계', '입력 cd/m²', '평균 광전자 e⁻/pixel', '평균 RAW DN',
          'ADC white 비율', '평균 전하 well 초과 비율'], levels.map(level => [
          String(level.index + 1), fmt(level.scene_luminance_cd_m2),
          fmt(level.mean_photoelectrons_per_pixel), fmt(level.mean_raw_dn),
          fmt(level.sampled_adc_white_fraction, 4), fmt(level.mean_charge_above_well_fraction, 4),
        ])));
        card.append(make('p', 'note', response.mean_charge_above_well_fraction_method || ''));
        host.append(card);
      }
    }
  }
  function renderSearch(summary) {
    const host = $('result-summary'); host.replaceChildren();
    const search = summary.search;
    host.append(make('h3', '', '노출·gain 후보 결과'));
    const selected = search.best_case;
    host.append(make('p', selected ? 'best' : 'warning', selected
      ? `선언한 제약을 만족한 ${search.feasible_count}/${search.case_count}개 후보 중 선택: ${fmt(selected.parameters?.['sensor.integration_time'] * 1000)} ms · gain ${fmt(selected.parameters?.['sensor.analog_gain'])} · 평균 ${fmt(selected.mean_electrons_e_per_pixel)} e⁻/pixel`
      : `제약을 만족한 후보가 없습니다 (${search.feasible_count}/${search.case_count}). 포화 상한 또는 후보를 검토하세요.`));
    host.append(table(['후보', '노출 (ms)', 'gain', '평균 e⁻/pixel', '평균 RAW DN',
      'well 포화', 'ADC white 포화', '제약'], search.cases.map(item => [
      String(item.case_index), fmt(item.parameters?.['sensor.integration_time'] * 1000),
      fmt(item.parameters?.['sensor.analog_gain']), fmt(item.mean_electrons_e_per_pixel),
      fmt(item.mean_raw_dn), fmt(item.capture?.saturation?.well_fraction, 4),
      fmt(item.capture?.saturation?.adc_white_fraction, 4), item.feasible ? '만족' : '불만족',
    ])));
    host.append(make('p', 'note', '평균 광전자는 gain 적용 전 값입니다. 이 탐색은 시간 SNR, 공간 화질, HDR/LFM이나 센서 제품 성능을 최적화하지 않습니다. 후보 RAW 배열은 저장하지 않습니다.'));
  }
  function safeArtifact(descriptor) {
    return descriptor && /^\/api\/v1\/artifacts\/[0-9a-f]{64}$/.test(descriptor.url || '');
  }
  function renderArtifacts(job) {
    const host = $('artifacts'); host.replaceChildren();
    const seen = new Set();
    for (const descriptor of job.artifacts || []) {
      if (!safeArtifact(descriptor) || seen.has(descriptor.sha256)) continue;
      seen.add(descriptor.sha256);
      const link = make('a', 'artifact', descriptor.name || '저장 산출물');
      link.href = descriptor.url; link.download = descriptor.name || 'artifact';
      const sha = make('span', '', `${descriptor.sha256.slice(0, 16)}… SHA-256`);
      link.append(sha); host.append(link);
    }
    if (!host.children.length) host.append(make('p', 'note', '이 작업에 저장된 산출물이 없습니다.'));
  }
  function renderJob(kind, job) {
    const summary = SensorWorkbenchSummary(kind, job);
    $('result').hidden = false;
    $('result-summary').replaceChildren();
    $('result-limits').textContent = summary?.limitation || job.limitation ||
      '완료된 계산 결과가 없습니다. 작업 상태와 오류를 확인하세요.';
    $('permalink').href = `?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(job.id)}`;
    $('export').href = `/api/v1/${paths[kind]}/${encodeURIComponent(job.id)}/export`;
    $('export').download = `${kind === 'quality' ? 'saved-sensor-quality' : 'saved-sensor-search'}.json`;
    $('cancel').hidden = !['queued', 'running'].includes(job.status);
    $('provenance').textContent = JSON.stringify(summary ? {
      specification: summary.specification,
      source_identities: summary.source_identities || summary.source_identity,
      upstream_assets: summary.upstream_assets,
      verification: summary.verification || summary.search?.verification,
      source_scope_policy: kind === 'quality' ? summary.sensor_reports.map(item => item.source_scope_policy) : summary.search?.source_inspection?.native_response?.source_scope_policy,
    } : {specification: job.specification, source_identity: job.source_identity || job.source_identities}, null, 2);
    renderArtifacts(job);
    if (summary) (kind === 'quality' ? renderQuality : renderSearch)(summary);
  }
  async function loadJob(kind, id) {
    if (!paths[kind] || !id) throw new Error('작업 종류와 ID를 입력하세요.');
    const mine = ++epoch; clearTimeout(timer); clearError();
    $('open-kind').value = kind; $('open-id').value = id;
    history.replaceState(null, '', `?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`);
    const job = await api(`${paths[kind]}/${encodeURIComponent(id)}`);
    if (mine !== epoch) return;
    if (job.id !== id || job.job_kind !== kinds[kind]) throw new Error('선택한 작업 ID와 저장 기록 종류가 일치하지 않습니다.');
    active = {kind, id};
    $('status').textContent = `${job.status} · ${id}${job.error ? ' · ' + job.error : ''}`;
    renderJob(kind, job);
    if (['queued', 'running'].includes(job.status)) {
      timer = setTimeout(() => loadJob(kind, id).catch(showError), 1500);
    } else {
      refreshRecent().catch(showError);
    }
  }
  async function refreshRecent() {
    const [quality, search] = await Promise.all([api(paths.quality), api(paths.search)]);
    const rows = [
      ...quality.map(item => ({...item, kind: 'quality'})),
      ...search.map(item => ({...item, kind: 'search'})),
    ].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || ''))).slice(0, 10);
    const host = $('recent'); host.replaceChildren();
    if (!rows.length) { host.append(make('p', 'note', '아직 저장된 작업이 없습니다.')); return; }
    for (const item of rows) {
      const button = make('button', 'recent-item', `${item.kind === 'quality' ? '화질 진단' : '노출·gain 탐색'} · ${item.name || item.id} · ${item.status}`);
      button.type = 'button';
      button.addEventListener('click', () => loadJob(item.kind, item.id).catch(showError));
      host.append(button);
    }
  }
  async function init() {
    const session = await api('session');
    if (session.schema !== 'camera_platform_v1' || !session.token) throw new Error('Camera Platform 세션을 확인할 수 없습니다.');
    token = session.token;
    const [quality, search] = await Promise.all([
      api(`${paths.quality}/template`), api(`${paths.search}/template`),
    ]);
    qualityTemplate = quality.specification; searchTemplate = search.specification;
    qualityCatalog = quality.sensor_catalog; searchCatalog = search.sensor_catalog;
    if (qualityTemplate?.schema !== SAVED_SENSOR_QUALITY_SCHEMA ||
        searchTemplate?.schema !== SAVED_SENSOR_SEARCH_SCHEMA ||
        !Array.isArray(qualityCatalog) || !Array.isArray(searchCatalog)) {
      throw new Error('설치된 서버와 화면의 저장 센서 입력 계약이 다릅니다.');
    }
    $('connection').textContent = '로컬 작업공간과 저장 센서 DB에 연결했습니다.';
    $('quality-name').value = qualityTemplate.name;
    $('quality-luminance').value = qualityTemplate.luminance_cd_m2;
    $('quality-exposure').value = qualityTemplate.exposure_s * 1000;
    $('quality-frames').value = qualityTemplate.frames;
    $('quality-rows').value = qualityTemplate.output_size[0];
    $('quality-cols').value = qualityTemplate.output_size[1];
    $('quality-seed').value = qualityTemplate.seed;
    $('quality-limits').textContent = quality.limitation;
    $('search-name').value = searchTemplate.name;
    $('search-exposures').value = searchTemplate.exposure_s.map(item => item * 1000).join(', ');
    $('search-gains').value = searchTemplate.analog_gain.join(', ');
    $('search-luminance').value = searchTemplate.luminance_cd_m2;
    $('search-ceiling').value = searchTemplate.max_saturation_fraction;
    $('search-rows').value = searchTemplate.sensor_size[0];
    $('search-cols').value = searchTemplate.sensor_size[1];
    $('search-seed').value = searchTemplate.seed;
    $('search-limits').textContent = search.limitation;
    refreshSensors();
    await refreshRecent();
    const query = new URLSearchParams(location.search), kind = query.get('kind'), id = query.get('id');
    if (kind && id) await loadJob(kind, id);
  }
  $('sensor-filter').addEventListener('input', refreshSensors);
  for (const form of [$('quality-form'), $('search-form')]) {
    form.addEventListener('input', updateForms);
    form.addEventListener('change', updateForms);
  }
  $('quality-form').addEventListener('submit', async event => {
    event.preventDefault(); if (qualitySubmitting) return;
    qualitySubmitting = true; updateForms(); clearError();
    try {
      const spec = SensorWorkbenchQualitySpecification(qualityTemplate, qualityCatalog, qualityInput());
      $('status').textContent = '저장 센서 run과 계산 조건 확인 후 제출 중…';
      const job = await api(paths.quality, spec);
      if (!job.id || job.job_kind !== kinds.quality) throw new Error('화질 진단 작업 ID를 받지 못했습니다.');
      await loadJob('quality', job.id);
    } catch (error) { showError(error); $('status').textContent = '제출 실패 · 조건과 서버 오류를 확인하세요.'; }
    finally { qualitySubmitting = false; updateForms(); }
  });
  $('search-form').addEventListener('submit', async event => {
    event.preventDefault(); if (searchSubmitting) return;
    searchSubmitting = true; updateForms(); clearError();
    try {
      const spec = SensorWorkbenchSearchSpecification(searchTemplate, searchCatalog, searchInput());
      $('status').textContent = '선언 후보와 저장 run 계약 확인 후 제출 중…';
      const job = await api(paths.search, spec);
      if (!job.id || job.job_kind !== kinds.search) throw new Error('탐색 작업 ID를 받지 못했습니다.');
      await loadJob('search', job.id);
    } catch (error) { showError(error); $('status').textContent = '제출 실패 · 후보와 서버 오류를 확인하세요.'; }
    finally { searchSubmitting = false; updateForms(); }
  });
  $('open-form').addEventListener('submit', event => {
    event.preventDefault(); loadJob(value('open-kind'), value('open-id').trim()).catch(showError);
  });
  $('refresh-list').addEventListener('click', () => refreshRecent().catch(showError));
  $('cancel').addEventListener('click', async () => {
    if (!active) return;
    const {kind, id} = active;
    try {
      await api(`${paths[kind]}/${encodeURIComponent(id)}/cancel`, {});
      await loadJob(kind, id);
    } catch (error) { showError(error); }
  });
  init().catch(error => {
    $('connection').textContent = '작업공간 또는 저장 센서 DB 연결에 실패했습니다.';
    showError(error); updateForms();
  });
})();

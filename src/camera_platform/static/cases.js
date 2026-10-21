'use strict';

globalThis.CameraCasePlanInputs = function (plan) {
  const conditions = plan.conditions || {}, exposures = [];
  let unknownExposure = false;
  for (const key of ['exposure', 'exposure_ms', 'exposure_s']) {
    if (!(key in conditions)) continue;
    const quantity = conditions[key], allowedUnits = key === 'exposure_ms' ? ['ms'] : key === 'exposure_s' ? ['s'] : ['ms', 's'];
    if (quantity && typeof quantity === 'object' && typeof quantity.value === 'number' && Number.isFinite(quantity.value) && allowedUnits.includes(quantity.unit)) exposures.push(quantity.value * (quantity.unit === 's' ? 1000 : 1));
    else unknownExposure = true;
  }
  const exposure = !unknownExposure && exposures.length && exposures.every(value => value === exposures[0]) && exposures[0] >= .01 && exposures[0] <= 10000 ? exposures[0] : null;
  const seed = Number.isInteger(conditions.seed) && conditions.seed >= 0 && conditions.seed <= 2 ** 31 - 1 ? conditions.seed : null;
  return {exposure, seed};
};

// Case state is independent of rendering. A draft and an uncertain submission
// survive refreshes; an old asynchronous reply cannot change a newer selection.
globalThis.CameraCaseState = function ({api, storage, storageKey, changed = () => {}, requestId = () => 'case-' + globalThis.crypto.randomUUID()}) {
  const clone = value => JSON.parse(JSON.stringify(value));
  const s = {selected: '', detail: null, history: [], draft: null, editing: false, pending: null, uncertain: false, saving: false, loading: false, error: '', notice: '', persistenceError: ''};
  let selection = 0, historyRequest = 0;
  try {
    const saved = JSON.parse(storage?.getItem(storageKey) || 'null');
    if (saved?.schema === 'camera_case_draft_v1') {
      s.selected = typeof saved.selected === 'string' ? saved.selected : '';
      s.draft = saved.draft || null; s.editing = Boolean(saved.editing && s.draft);
      s.pending = saved.pending || null;
      s.uncertain = Boolean(s.pending);
      if (s.pending) { s.draft = clone(s.pending); s.editing = true; s.notice = '이전 저장 응답을 확인하지 못했습니다. 같은 내용으로 저장을 다시 확인하세요.'; }
    }
  } catch (_) { s.persistenceError = '브라우저에 보존한 초안을 읽지 못했습니다. 이 탭을 닫기 전에 초안을 내보내세요.'; }
  function persist() {
    try {
      if (!storage) throw Error('storage unavailable');
      storage.setItem(storageKey, JSON.stringify({schema: 'camera_case_draft_v1', selected: s.selected, draft: s.draft, editing: s.editing, pending: s.pending}));
      s.persistenceError = '';
    } catch (_) { s.persistenceError = '브라우저 초안 보존을 사용할 수 없습니다. 새로고침 전에 초안을 내보내세요.'; }
  }
  function notify() { persist(); changed(); }
  function validDetail(value, id) {
    if (value?.id !== id || value.schema !== 'camera_case_review_v1' || value._projection || !value.snapshots || !Array.isArray(value.snapshots.evidence)) throw Error('선택한 사례의 원본 응답이 일치하지 않습니다.');
    return value;
  }
  async function select(id) {
    const generation = ++selection; ++historyRequest;
    s.selected = id; s.detail = null; s.history = []; s.loading = Boolean(id); s.error = ''; notify();
    if (!id) return null;
    try {
      const detail = validDetail(await api('/case_reviews/' + encodeURIComponent(id)), id);
      if (generation !== selection) return null;
      s.detail = detail; return detail;
    } catch (error) { if (generation === selection) s.error = error.message; return null; }
    finally { if (generation === selection) { s.loading = false; notify(); } }
  }
  async function loadHistory() {
    if (!s.detail) return;
    const id = s.detail.id, caseId = s.detail.case_id, generation = selection, request = ++historyRequest;
    try {
      const rows = await api('/case_reviews/' + encodeURIComponent(id) + '/history');
      if (generation !== selection || request !== historyRequest) return;
      if (!Array.isArray(rows) || rows.some(row => row.case_id !== caseId)) throw Error('선택한 사례의 리뷰 이력이 일치하지 않습니다.');
      s.history = rows; s.error = '';
    } catch (error) { if (generation === selection && request === historyRequest) s.error = error.message; }
    finally { if (generation === selection && request === historyRequest) notify(); }
  }
  function begin(record = null) {
    if (s.draft) { s.editing = true; s.notice = '작성 중인 초안을 먼저 저장하거나 폐기하세요. 기존 초안을 유지했습니다.'; notify(); return false; }
    s.draft = record ? {
      previous_revision_id: record.id, name: record.name, question: record.question,
      requirements: clone(record.requirements), assembly_ids: clone(record.assembly_ids), evidence: clone(record.evidence),
      decision: clone(record.decision), unresolved: clone(record.unresolved), next_experiments: clone(record.next_experiments)
    } : {previous_revision_id: null, name: '', question: '', requirements: [], assembly_ids: [], evidence: [], decision: {status: 'open', rationale: ''}, unresolved: [], next_experiments: []};
    s.editing = true; s.error = ''; s.notice = ''; notify(); return true;
  }
  function edit(update) {
    if (!s.draft || s.pending || s.saving) return false;
    update(s.draft); delete s.draft.request_id; s.notice = ''; persist(); return true;
  }
  function discard() {
    if (s.pending || s.saving) return false;
    s.draft = null; s.editing = false; s.error = ''; s.notice = ''; notify(); return true;
  }
  async function save() {
    if (!s.draft || s.saving) return null;
    // Persist the exact request before sending. A lost response never generates
    // a new request identity, even after a browser reload.
    if (!s.pending) s.pending = {...clone(s.draft), request_id: requestId()};
    const payload = clone(s.pending), generation = selection, hadUncertainAttempt = s.uncertain;
    s.saving = true; s.error = ''; notify();
    try {
      const result = await api('/case_reviews', payload);
      validDetail(result, result?.id);
      if (result.request_id !== payload.request_id) throw Error('저장 응답의 요청 식별자가 다릅니다. 같은 요청으로 다시 확인하세요.');
      s.draft = null; s.pending = null; s.uncertain = false; s.editing = false; s.notice = '불변 리뷰를 저장했습니다. 판단은 사용자 검토 기록이며 출시 승인이 아닙니다.';
      if (generation === selection) { ++selection; ++historyRequest; s.selected = result.id; s.detail = result; s.history = []; s.loading = false; }
      return result;
    } catch (error) {
      // An initial validation/conflict response is definitive. A rejection of
      // a later retry cannot disprove a previous uncertain attempt's commit.
      if (!hadUncertainAttempt && error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) s.pending = null;
      s.uncertain = Boolean(s.pending);
      s.error = error.message + (s.pending ? ' 저장 여부가 불확실합니다. 입력을 유지한 채 같은 요청으로 다시 확인하세요.' : ' 초안은 유지했습니다. 내용을 수정하거나 최신 리뷰를 확인하세요.');
      return null;
    } finally { s.saving = false; notify(); }
  }
  return {s, select, loadHistory, begin, edit, discard, save, notify};
};

globalThis.CameraCaseUI = function ({api, esc, records, state, render, refresh, toast, downloadObject, openTarget}) {
  const array = value => Array.isArray(value) ? value : [];
  const clone = value => JSON.parse(JSON.stringify(value));
  const decisions = {open: '검토 시작', investigate: '추가 조사', candidate: '설계 후보', rejected: '채택하지 않음', deferred: '판단 보류'};
  const modes = {quick_estimate: '빠른 추정', response_db: '응답 DB 계산', surrogate_prediction: 'Surrogate 예측', detailed_characterization: '상세 특성 계산', measurement: '실측'};
  const kinds = {runs: '계산', measurements: '관측 데이터', validations: '비교 검증', studies: '설계 실험', isp_studies: '고정 RAW의 ISP 탐색'};
  let model = null, workspace = '', index = [], autoLoad = '', discardPrompt = false;
  const conditionRows = new WeakMap();
  function sync() {
    const key = String(state.data?.state_dir || '') + ':' + String(state.data?.workspace || '');
    if (!model || key !== workspace) {
      workspace = key; autoLoad = ''; index = [];
      let storage; try { storage = globalThis.sessionStorage; } catch (_) { /* visible persistence warning on first edit */ }
      model = CameraCaseState({api, storage, storageKey: 'camera-case-v1:' + workspace, changed: () => { if (state.view === 'overview') render(); }});
    }
    index = records('case_reviews');
    const s = model.s;
    const unavailable = index.find(r => r.id === s.selected)?.availability?.status === 'unavailable';
    if (unavailable && (s.detail || s.loading)) { autoLoad = s.selected; void model.select(s.selected); }
    else if (!s.selected && index.length && !s.draft) { autoLoad = index[0].id; void model.select(index[0].id); }
    else if (s.selected && !s.detail && !s.loading && autoLoad !== s.selected) { autoLoad = s.selected; void model.select(s.selected); }
  }
  const button = (action, label, attrs = '', style = 'secondary') => `<button type="button" class="button ${style}" data-case-action="${action}" ${attrs}>${esc(label)}</button>`;
  const option = (id, label, selected) => `<option value="${esc(id)}" ${selected ? 'selected' : ''}>${esc(label)}</option>`;
  const label = item => `${item.name || item.id}${item.revision ? ' · r' + item.revision : ''}${item.availability?.status === 'unavailable' ? ' · 근거 확인 불가' : ''}`;
  function choices(collection, selected, prompt = '선택 안 함', filter = () => true) {
    const rows = records(collection).filter(filter);
    return option('', prompt, !selected) + (!rows.some(row => row.id === selected) && selected ? option(selected, selected + ' · 현재 목록에 없음', true) : '') + rows.map(item => option(item.id, label(item), item.id === selected)).join('');
  }
  function field(name, title, value, {type = 'text', index: i, sub, options, multiline = false, required = false, max = 2000} = {}) {
    const id = 'case-' + name + (i === undefined ? '' : '-' + i) + (sub === undefined ? '' : '-' + sub);
    const attrs = `id="${id}" data-case-field="${name}" ${i === undefined ? '' : `data-index="${i}"`} ${sub === undefined ? '' : `data-sub="${sub}"`} ${required ? 'required' : ''}`;
    return `<div class="form-field"><label for="${id}">${esc(title)}</label>${options !== undefined ? `<select ${attrs}>${options}</select>` : multiline ? `<textarea ${attrs} rows="3" maxlength="${max}">${esc(value)}</textarea>` : `<input ${attrs} type="${type}" value="${esc(value)}" maxlength="${max}">`}</div>`;
  }
  function ispEvidenceScope(e, i, draft) {
    const study = records('isp_studies').find(row => row.id === e.id);
    if (!study) return '<p class="case-error">이 ISP 탐색의 자료별 시험을 불러올 수 없습니다. 새로고침 후 확인하세요.</p>';
    return `<div class="case-requirement-links"><h4>자료별로 확인하는 요구사항</h4><p class="muted">각 RAW 취득의 원래 시험과 같은 요구사항만 연결할 수 있습니다.</p>${array(study.datasets).map(dataset => `<fieldset><legend>${esc(dataset.key)} · ${esc(dataset.test_id || '고정 시험 없음')}</legend>${draft.requirements.map(r => `<label class="check-option"><input type="checkbox" id="case-isp-link-${i}-${esc(dataset.key)}-${esc(r.key)}" data-case-field="isp-dataset-requirement" data-index="${i}" data-dataset="${esc(dataset.key)}" value="${esc(r.key)}" ${array(e.dataset_requirements?.[dataset.key]).includes(r.key)?'checked':''} ${!r.test_id || r.test_id!==dataset.test_id?'disabled':''}><span>${esc(r.description || r.key)}${r.test_id!==dataset.test_id?' · 취득 시험 다름':''}</span></label>`).join('') || '<p>요구사항과 고정 시험을 먼저 추가하세요.</p>'}</fieldset>`).join('')}${field('isp-selection','선택한 ISP 후보 · 선택 사항',e.selection_id,{index:i,options:choices('isp_selections',e.selection_id || '','선택 근거 연결 안 함',selection=>selection.study_id===e.id || study.specification?.selection_id===selection.id)})}</div>`;
  }
  function unlinkRequirement(e, key) {
    e.requirement_keys = e.requirement_keys.filter(k => k !== key);
    if (e.kind === 'isp_studies') {
      for (const [dataset, keys] of Object.entries(e.dataset_requirements || {})) {
        const kept = keys.filter(k => k !== key);
        if (kept.length) e.dataset_requirements[dataset] = kept;
        else delete e.dataset_requirements[dataset];
      }
    }
  }
  function human(value) {
    if (value === null || value === undefined) return '—';
    if (Array.isArray(value)) return value.map(human).join(' · ');
    if (typeof value === 'object') return Object.entries(value).map(([key, entry]) => key + ': ' + human(entry)).join(' / ');
    return String(value);
  }
  function rowsFor(plan) {
    if (!conditionRows.has(plan)) conditionRows.set(plan, Object.entries(plan.conditions).map(([key, value]) => {
      const quantity = value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).every(k => ['value', 'unit'].includes(k)) && 'value' in value;
      const raw = quantity ? value.value : value;
      return {key, value: typeof raw === 'object' ? human(raw) : String(raw), type: typeof raw === 'number' ? 'number' : typeof raw === 'boolean' ? 'boolean' : typeof raw === 'object' ? 'preserved' : 'text', unit: quantity ? value.unit || '' : '', original: clone(value)};
    }));
    return conditionRows.get(plan);
  }
  function conditionsFrom(rows) {
    const result = {};
    for (const row of rows) {
      const key = row.key.trim();
      if (!key) throw Error('다음 실험의 조건 이름을 입력하세요.');
      if (Object.hasOwn(result, key) || ['__proto__', 'constructor', 'prototype'].includes(key)) throw Error('조건 이름은 중복 없이 입력하세요.');
      let value = row.type === 'preserved' ? clone(row.original) : row.type === 'number' ? Number(row.value) : row.type === 'boolean' ? row.value === 'true' : row.value;
      if (row.type === 'number' && (!row.value.trim() || !Number.isFinite(value))) throw Error('숫자 조건에 유한한 숫자를 입력하세요.');
      if (row.type !== 'preserved' && row.unit.trim()) value = {value, unit: row.unit.trim()};
      result[key] = value;
    }
    return result;
  }
  function persistConditions(plan, rows) {
    // Keep incomplete condition edits in the draft across reload, without ever
    // sending editor-only fields to the API.
    plan._condition_rows = clone(rows);
  }
  function editForm() {
    const s = model.s, d = s.draft;
    if (!d || !s.editing) return '';
    const frozen = Boolean(s.pending || s.saving);
    return `<section class="panel case-editor" aria-label="사례 리뷰 작성"><div class="panel-header"><div><span class="eyebrow">${d.previous_revision_id ? 'NEW REVIEW REVISION' : 'NEW ENGINEERING CASE'}</span><h2>${d.previous_revision_id ? '새 리뷰 작성' : '새 사례 작성'}</h2><p>원본 리뷰를 보존하고 새 버전으로 저장합니다. 작성 내용은 이 탭에 보존됩니다.</p></div>${button('hide-draft', '접어두기')}</div><div class="panel-content"><form id="case-review-form"><fieldset ${frozen ? 'disabled' : ''} class="case-fields">
      ${field('name', '사례 이름', d.name, {required: true, max: 200})}${field('question', '해결할 설계 질문', d.question, {required: true, multiline: true})}
      <section class="case-form-section"><h3>요구사항과 고정 시험</h3><p class="muted">시험은 선택 사항입니다. 근거를 연결할 요구사항에는 해당 근거와 같은 시험을 고정하세요.</p>${d.requirements.map((r, i) => `<div class="case-form-row">${field('requirement-description', `요구사항 ${i + 1}`, r.description, {index: i, required: true, multiline: true})}${field('requirement-test', '연결할 고정 시험', r.test_id, {index: i, options: choices('tests', r.test_id)})}${button('remove-requirement', '요구사항 삭제', `data-index="${i}"`, 'quiet')}</div>`).join('')}${button('add-requirement', '요구사항 추가')}</section>
      <section class="case-form-section"><h3>검토할 카메라 구성</h3><p class="muted">이 사례에서 검토할 버전만 선택하세요.</p><div class="case-choices">${records('assemblies').map(a => `<label class="check-option"><input type="checkbox" id="case-assembly-${esc(a.id)}" data-case-field="assembly" value="${esc(a.id)}" ${d.assembly_ids.includes(a.id) ? 'checked' : ''}><span><strong>${esc(label(a))}</strong><small>${esc(a.id)}</small></span></label>`).join('') || '<p>등록된 카메라 구성이 없습니다. 구성 없이 초안을 저장할 수 있습니다.</p>'}</div></section>
      <section class="case-form-section"><h3>판단에 사용할 근거</h3><p class="muted">전체 목록에서 직접 선택한 기록만 이 사례에 연결됩니다. 실행 완료 기록과 측정·검증의 원본을 저장 시점에 고정합니다.</p><div class="form-row">${field('evidence-kind', '근거 종류', '', {options: Object.entries(kinds).map(([k, v]) => option(k, v, k === (s.evidenceKind || 'runs'))).join('')})}${field('evidence-choice', '연결할 기록', '', {options: choices(s.evidenceKind || 'runs', s.evidenceChoice || '', '기록 선택', r => (s.evidenceKind || 'runs') !== 'runs' || ['succeeded', 'failed', 'cancelled'].includes(r.status))})}</div>${button('add-evidence', '선택한 근거 연결')}${d.evidence.map((e, i) => `<div class="case-form-row"><h4>${esc(kinds[e.kind])} · ${esc(records(e.kind).find(r => r.id === e.id)?.name || e.id)}</h4><p class="muted">${esc(e.id)}</p>${e.kind==='isp_studies'?ispEvidenceScope(e,i,d):`<fieldset class="case-requirement-links"><legend>이 근거가 확인하는 요구사항</legend>${d.requirements.map(r => `<label class="check-option"><input type="checkbox" id="case-evidence-link-${i}-${esc(r.key)}" data-case-field="evidence-requirement" data-index="${i}" value="${esc(r.key)}" ${e.requirement_keys.includes(r.key) ? 'checked' : ''} ${!r.test_id ? 'disabled' : ''}><span>${esc(r.description || r.key)}${!r.test_id ? ' · 시험을 먼저 선택하세요' : ''}</span></label>`).join('') || '<p>요구사항과 시험을 먼저 추가하세요.</p>'}</fieldset>`}${button('remove-evidence', '연결 해제', `data-index="${i}"`, 'quiet')}</div>`).join('')}</section>
      <section class="case-form-section"><h3>현재 판단과 남은 문제</h3>${field('decision-status', '판단 상태', d.decision.status, {options: Object.entries(decisions).map(([k, v]) => option(k, v, k === d.decision.status)).join('')})}${field('decision-rationale', '판단 이유', d.decision.rationale, {required: true, multiline: true, max: 4000})}${field('unresolved', '남은 문제 · 한 줄에 하나', d.unresolved.join('\n'), {multiline: true, max: 16000})}</section>
      <section class="case-form-section"><h3>다음에 확인할 실험</h3><p class="muted">계획을 기록합니다. 저장만으로 실험을 실행하지 않습니다.</p>${d.next_experiments.map((p, i) => {
        if (p._condition_rows) conditionRows.set(p, p._condition_rows);
        const rows = rowsFor(p);
        return `<div class="case-form-row">${field('plan-question', `다음 실험 ${i + 1} · 질문`, p.question, {index: i, required: true, multiline: true})}${field('plan-reason', '이 실험이 필요한 이유', p.reason, {index: i, required: true, multiline: true})}${field('plan-mode', '평가 방식', p.mode, {index: i, options: Object.entries(modes).map(([k, v]) => option(k, v, k === p.mode)).join('')})}<div class="form-row">${field('plan-assembly', '사용할 카메라', p.assembly_id, {index: i, options: choices('assemblies', p.assembly_id, '아직 정하지 않음', a => d.assembly_ids.includes(a.id))})}${field('plan-test', '사용할 시험', p.test_id, {index: i, options: choices('tests', p.test_id, '아직 정하지 않음', t => d.requirements.some(r => r.test_id === t.id))})}</div><h4>확인할 조건</h4>${rows.map((c, j) => `<div class="case-condition"><div class="form-row">${field('condition-key', '조건 이름', c.key, {index: i, sub: j, required: true})}${field('condition-type', '값 유형', c.type, {index: i, sub: j, options: (c.type === 'preserved' ? option('preserved', '원본 복합 조건 유지', true) : Object.entries({text: '문자', number: '숫자', boolean: '참 / 거짓'}).map(([k, v]) => option(k, v, k === c.type)).join(''))})}</div>${c.type === 'preserved' ? `<p class="case-preserved">${esc(c.value)}</p>` : `<div class="form-row">${field('condition-value', '조건 값', c.value, {index: i, sub: j, ...(c.type === 'boolean' ? {options: option('true', '참', c.value === 'true') + option('false', '거짓', c.value !== 'true')} : {})})}${field('condition-unit', '단위 · 선택', c.unit, {index: i, sub: j, max: 80})}</div>`}${button('remove-condition', '조건 삭제', `data-index="${i}" data-sub="${j}"`, 'quiet')}</div>`).join('')}<div class="case-actions">${button('add-condition', '조건 추가', `data-index="${i}"`)}${button('remove-plan', '실험 계획 삭제', `data-index="${i}"`, 'quiet')}</div></div>`;
      }).join('')}${button('add-plan', '다음 실험 추가')}</section></fieldset>
      <div class="case-boundary">이 판단은 사용자가 남기는 설계 검토 기록입니다. 실물 검증이나 출시 승인을 뜻하지 않습니다.</div><div class="case-actions"><button type="submit" class="button primary" ${s.saving ? 'disabled' : ''}>${s.saving ? '저장 확인 중…' : s.pending ? '같은 요청으로 저장 확인' : '불변 리뷰 저장'}</button>${button('export-draft', '초안 내보내기')}${!s.pending ? button('discard-prompt', '초안 폐기', s.saving ? 'disabled' : '', 'quiet') : ''}</div>${discardPrompt ? `<div class="callout" role="alert">이 탭의 작성 중인 초안을 폐기합니다. 저장된 리뷰는 유지됩니다.<div class="case-actions">${button('discard-confirm', '초안 폐기 확인')}${button('discard-cancel', '계속 작성', '', 'quiet')}</div></div>` : ''}</form></div></section>`;
  }
  function snapshotDetail(detail) {
    return `<details class="case-snapshot-detail"><summary>고정된 원본과 연결 관계 보기</summary><p>저장 시점의 원본을 이 리뷰와 함께 보존했습니다. 전체 내용은 리뷰 JSON으로 내보낼 수 있습니다.</p><dl class="case-facts"><dt>사례</dt><dd>${esc(detail.case_id)}</dd><dt>리뷰</dt><dd>${esc(detail.id)} · r${esc(detail.revision)}</dd><dt>이전 리뷰</dt><dd>${esc(detail.previous_revision_id || '첫 리뷰')}</dd><dt>저장 시각</dt><dd>${esc(detail.created_at)}</dd><dt>리뷰 내용 해시</dt><dd>${esc(detail.content_hash)}</dd></dl>${detail.snapshots.evidence.map(e => `<div class="case-form-row"><h4>${esc(kinds[e.kind])} · ${esc(e.snapshot.name || e.id)}</h4>${provenance(e)}<p class="case-hash">${esc(e.record_sha256)}</p><dl class="case-facts"><dt>실행 상태</dt><dd>${esc(e.snapshot.status || '등록됨')}</dd><dt>고정 시험</dt><dd>${esc(e.snapshot.test_id || e.snapshot.specification?.test_id || '연결 원본 참조')}</dd><dt>고정 카메라</dt><dd>${esc(e.snapshot.assembly_id || array(e.snapshot.specification?.assembly_ids).join(', ') || '연결 원본 참조')}</dd></dl><details><summary>원본 필드 전체 보기</summary>${recordFields(e.snapshot)}</details>${array(e.linked_records).map(r => `<details><summary>${esc(r.kind)} · ${esc(r.snapshot.name || r.id)}</summary><p class="case-hash">${esc(r.record_sha256)}</p>${recordFields(r.snapshot)}</details>`).join('')}</div>`).join('')}<details><summary>고정 카메라 · 시험 원본</summary>${detail.snapshots.assemblies.map(recordFields).join('')}${detail.snapshots.tests.map(recordFields).join('')}</details></details>`;
  }
  function recordFields(record) {
    // Read-only source details; authoring never requires implementation JSON.
    return `<dl class="case-facts">${Object.entries(record).map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(human(value))}</dd>`).join('')}</dl>`;
  }
  function provenance(evidence) {
    const names = {measured: '실측 원본', simulated: '계산으로 생성한 관측', estimated: '추정한 관측', vendor: '제공 자료'};
    const observations = evidence.kind === 'measurements' ? [evidence.snapshot] : array(evidence.linked_records).filter(r => r.kind === 'measurements').map(r => r.snapshot);
    return observations.map(r => `<small class="case-provenance">관측 출처 · ${esc(names[r.source_kind] || r.source_kind || '미확인')} · ${esc(r.name || r.id)}</small>`).join('');
  }
  function summary(detail) {
    const count = kind => detail.evidence.filter(e => e.kind === kind).length;
    return `<section class="case-ribbon" aria-label="현재 설계 사례" data-case-review="${esc(detail.id)}"><div class="ribbon-cell case-title"><div><span class="ribbon-label">SAVED ENGINEERING CASE · r${esc(detail.revision)}</span><span class="ribbon-value">${esc(detail.name)}</span></div></div><div class="ribbon-cell"><span class="ribbon-label">연결된 요구사항 · 구성</span><span class="ribbon-value">${detail.requirements.length} 요구사항 · ${detail.assembly_ids.length} 카메라</span></div><div class="ribbon-cell"><span class="ribbon-label">이 리뷰의 근거</span><span class="ribbon-value" data-case-counts>${Object.entries(kinds).map(([k, v]) => count(k) + ' ' + v).join(' · ')}</span></div><div class="ribbon-cell"><span class="ribbon-label">사용자가 기록한 판단</span><span class="ribbon-value">${esc(decisions[detail.decision.status])}</span></div></section><div class="case-layout"><div class="stack"><section class="panel"><div class="panel-header"><div><span class="eyebrow">DESIGN QUESTION</span><h2>${esc(detail.question)}</h2></div></div><div class="panel-content"><h3>요구사항과 시험</h3>${detail.requirements.length ? detail.requirements.map(r => `<div class="case-item"><strong>${esc(r.description)}</strong><p>${esc(detail.snapshots.tests.find(t => t.id === r.test_id)?.name || (r.test_id ? r.test_id : '시험 미정'))}</p></div>`).join('') : '<p class="muted">요구사항을 아직 기록하지 않았습니다.</p>'}<h3>버전이 고정된 카메라</h3>${detail.snapshots.assemblies.map(a => `<div class="case-item"><div><strong>${esc(label(a))}</strong><small>${esc(a.id)}</small></div>${button('open-assembly', '구성 열기', `data-id="${esc(a.id)}"`, 'quiet')}</div>`).join('') || '<p class="muted">카메라 구성을 아직 선택하지 않았습니다.</p>'}</div></section><section class="panel"><div class="panel-header"><h2>이 판단에 연결된 근거</h2></div><div class="panel-content">${detail.snapshots.evidence.map((e, i) => `<div class="case-item"><div><strong>${esc(kinds[e.kind])} · ${esc(e.snapshot.name || e.id)}</strong><small>${esc(e.id)}</small><p>${e.requirement_keys.map(k => esc(detail.requirements.find(r => r.key === k)?.description || k)).join(' · ')}</p><small>${esc(e.snapshot.status || '등록됨')} · 저장 시점 원본 보존</small>${provenance(e)}</div>${e.kind === 'runs' ? button('open-run', '결과 열기', `data-index="${i}"`, 'quiet') : e.kind==='isp_studies'?button('open-isp-study','ISP 비교 열기',`data-index="${i}"`,'quiet'):''}</div>`).join('') || '<p class="muted">연결된 근거가 없습니다. 이 사례의 검증 결과를 아직 판단할 수 없습니다.</p>'}${snapshotDetail(detail)}</div></section></div><div class="stack"><section class="panel"><div class="panel-header"><h2>판단과 남은 문제</h2></div><div class="panel-content"><span class="badge orange">${esc(decisions[detail.decision.status])}</span><p class="case-prose" data-case-decision>${esc(detail.decision.rationale)}</p><h3>남은 문제</h3>${detail.unresolved.length ? `<ul class="case-list">${detail.unresolved.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : '<p class="muted">기록된 남은 문제가 없습니다. 검증 완료를 뜻하지 않습니다.</p>'}<div class="case-boundary">사용자 설계 판단 · 실물 검증 아님 · 출시 승인 아님</div></div></section><section class="panel"><div class="panel-header"><h2>다음 실험 계획</h2></div><div class="panel-content">${detail.next_experiments.map((p, i) => `<div class="case-item case-plan"><span class="eyebrow">${esc(modes[p.mode])}</span><h3>${esc(p.question)}</h3><p>${esc(p.reason)}</p><dl class="case-facts"><dt>카메라</dt><dd>${esc(detail.snapshots.assemblies.find(a => a.id === p.assembly_id)?.name || p.assembly_id || '미정')}</dd><dt>시험</dt><dd>${esc(detail.snapshots.tests.find(t => t.id === p.test_id)?.name || p.test_id || '미정')}</dd>${Object.entries(p.conditions).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(human(v))}</dd>`).join('')}</dl>${p.assembly_id && p.test_id && ['quick_estimate', 'response_db'].includes(p.mode) ? button('open-plan', '이 구성·시험으로 실험 준비', `data-index="${i}"`) : '<p class="muted">기록된 계획입니다. 해당 도구에서 조건을 확인한 뒤 실행하세요.</p>'}</div>`).join('') || '<p class="muted">다음에 확인할 실험을 새 리뷰에 기록하세요.</p>'}</div></section></div></div>`;
  }
  function view() {
    if (!model) return '<p>사례를 준비하고 있습니다.</p>';
    const s = model.s, selected = s.detail;
    const options = [...index];
    if (selected && !options.some(r => r.id === selected.id)) options.push(selected);
    return `<div class="case-workspace"><div class="page-heading"><div><span class="eyebrow">ENGINEERING CASES</span><h1>설계 질문에서 다음 실험까지</h1><p>요구사항, 고정 설계, 근거와 판단을 하나의 사례로 이어갑니다.</p></div><div class="heading-actions">${button('new', '새 사례 작성', '', 'primary')}</div></div><section class="case-toolbar"><div class="form-field"><label for="case-selection">저장된 사례 · 최신 리뷰</label><select id="case-selection" data-case-select>${option('', '저장된 사례 선택', !s.selected)}${options.map(r => option(r.id, label(r), r.id === s.selected)).join('')}</select></div><div class="case-actions">${selected ? button('revise', '새 리뷰 작성') + button('history', '이전 리뷰 보기') + button('export', '리뷰 JSON 내보내기') : ''}${s.draft && !s.editing ? button('resume', '작성 중인 초안 이어가기') : ''}</div></section>${s.error ? `<div class="case-error" role="alert">${esc(s.error)}${!s.editing ? `<div class="case-actions">${button('retry', '선택한 리뷰 다시 불러오기')}</div>` : ''}</div>` : ''}${s.persistenceError ? `<div class="case-error" role="alert">${esc(s.persistenceError)}</div>` : ''}${s.notice ? `<p class="case-notice" role="status">${esc(s.notice)}</p>` : ''}${s.history.length ? `<section class="panel case-history"><div class="panel-header"><h2>이 사례의 리뷰 이력</h2></div><div class="panel-content">${s.history.map(r => `<div class="case-item"><div><strong>r${esc(r.revision)} · ${esc(r.name)}</strong><small>${esc(r.created_at)} · ${esc(decisions[r.decision.status])}</small></div>${button('select', r.id === s.selected ? '현재 표시 중' : '이 리뷰 보기', `data-id="${esc(r.id)}" ${r.id === s.selected ? 'disabled' : ''}`, 'quiet')}</div>`).join('')}</div></section>` : ''}${editForm()}${s.loading ? '<div class="loading-state" role="status">선택한 리뷰 원본을 불러오고 있습니다.</div>' : selected ? summary(selected) : !s.editing ? `<div class="empty-state" data-case-empty><h2>${index.length ? '검토할 사례를 선택하세요' : '저장된 설계 사례가 없습니다'}</h2><p>설계 질문과 현재 판단을 기록하고 필요한 구성과 근거를 연결하세요. 다른 사례의 계산이나 검증 결과는 자동으로 연결되지 않습니다.</p>${button('new', '새 사례 작성', '', 'primary')}</div>` : ''}</div>`;
  }
  function prepareSubmission() {
    const d = model.s.draft;
    if (!d || model.s.pending) return;
    model.edit(value => { value.unresolved = value.unresolved.map(line => line.trim()).filter(Boolean); for (const plan of value.next_experiments) { plan.conditions = conditionsFrom(plan._condition_rows || rowsFor(plan)); delete plan._condition_rows; } });
    for (const e of d.evidence) if (!e.requirement_keys.length) throw Error('각 근거가 확인하는 요구사항을 하나 이상 연결하세요.');
  }
  document.addEventListener('input', event => {
    const el = event.target.closest('[data-case-field]'); if (!el || !model) return;
    const name = el.dataset.caseField, i = Number(el.dataset.index), j = Number(el.dataset.sub), s = model.s;
    if (name === 'evidence-kind') { s.evidenceKind = el.value; s.evidenceChoice = ''; render(); return; }
    if (name === 'evidence-choice') { s.evidenceChoice = el.value; return; }
    const rerender = ['assembly', 'requirement-test', 'condition-type'].includes(name);
    model.edit(d => {
      if (['name', 'question'].includes(name)) d[name] = el.value;
      else if (name === 'unresolved') d.unresolved = el.value.split('\n');
      else if (name === 'decision-status') d.decision.status = el.value;
      else if (name === 'decision-rationale') d.decision.rationale = el.value;
      else if (name === 'assembly') d.assembly_ids = el.checked ? [...new Set([...d.assembly_ids, el.value])] : d.assembly_ids.filter(id => id !== el.value);
      else if (name === 'requirement-description') d.requirements[i].description = el.value;
      else if (name === 'requirement-test') { d.requirements[i].test_id = el.value || null; for (const e of d.evidence) unlinkRequirement(e, d.requirements[i].key); }
      else if (name === 'evidence-requirement') { const e = d.evidence[i]; e.requirement_keys = el.checked ? [...new Set([...e.requirement_keys, el.value])] : e.requirement_keys.filter(key => key !== el.value); }
      else if (name === 'isp-dataset-requirement') {
        const e = d.evidence[i], dataset = el.dataset.dataset;
        if (e.kind !== 'isp_studies') return;
        e.dataset_requirements ||= {};
        const keys = array(e.dataset_requirements[dataset]);
        const updated = el.checked ? [...new Set([...keys, el.value])] : keys.filter(key => key !== el.value);
        if (updated.length) e.dataset_requirements[dataset] = updated; else delete e.dataset_requirements[dataset];
        e.requirement_keys = [...new Set(Object.values(e.dataset_requirements).flat())];
      }
      else if (name === 'isp-selection') d.evidence[i].selection_id = el.value || null;
      else if (name.startsWith('plan-')) { const key = name.slice(5); d.next_experiments[i][{assembly: 'assembly_id', test: 'test_id'}[key] || key] = ['assembly', 'test'].includes(key) ? el.value || null : el.value; }
      else if (name.startsWith('condition-')) { const plan = d.next_experiments[i], rows = plan._condition_rows || rowsFor(plan); rows[j][name.slice(10)] = el.value; if (name === 'condition-type' && el.value === 'boolean') rows[j].value = 'false'; persistConditions(plan, rows); }
    });
    if (rerender) render();
  });
  document.addEventListener('change', event => {
    if (event.target.matches('[data-case-select]') && model) { autoLoad = event.target.value; void model.select(event.target.value); }
  });
  document.addEventListener('submit', event => {
    if (event.target.id !== 'case-review-form') return;
    event.preventDefault();
    void (async () => {
      try {
        prepareSubmission();
        const saved = await model.save();
        if (saved) {
          state.data.case_reviews = [saved, ...records('case_reviews').filter(r => r.case_id !== saved.case_id)]; index = records('case_reviews');
          try { await refresh(); } catch (error) { model.s.error = '리뷰는 저장했지만 목록을 새로고침하지 못했습니다: ' + error.message; render(); }
        }
      } catch (error) { model.s.error = error.message; render(); }
    })();
  });
  document.addEventListener('click', event => {
    const el = event.target.closest('[data-case-action]'); if (!el || !model || el.disabled) return;
    const action = el.dataset.caseAction, i = Number(el.dataset.index), j = Number(el.dataset.sub), s = model.s;
    void (async () => {
      try {
        if (action === 'new') { discardPrompt = false; model.begin(); return; }
        if (action === 'revise') { discardPrompt = false; if (s.detail) model.begin(s.detail); return; }
        if (action === 'resume' || action === 'hide-draft') { s.editing = action === 'resume'; model.notify(); return; }
        if (action === 'select' || action === 'retry') { autoLoad = el.dataset.id || s.selected; await model.select(autoLoad); return; }
        if (action === 'history') { await model.loadHistory(); return; }
        if (action === 'export') { if (s.detail) downloadObject(s.detail, s.detail.id + '.json'); return; }
        if (action === 'export-draft') { if (s.draft) downloadObject(s.pending || s.draft, 'camera-case-unsaved-draft.json'); return; }
        if (action === 'discard-prompt' || action === 'discard-cancel') { discardPrompt = action === 'discard-prompt'; render(); return; }
        if (action === 'discard-confirm') { discardPrompt = false; model.discard(); return; }
        if (action.startsWith('open-')) {
          const detail = s.detail; if (!detail) return;
          if (action === 'open-assembly') { const record = detail.snapshots.assemblies.find(a => a.id === el.dataset.id); if (record) await openTarget({kind: 'assembly', review: detail, record, isCurrent: () => s.detail?.id === detail.id}); }
          if (action === 'open-run') { const evidence = detail.snapshots.evidence[i]; if (evidence?.kind === 'runs') await openTarget({kind: 'run', review: detail, record: evidence.snapshot, isCurrent: () => s.detail?.id === detail.id}); }
          if (action === 'open-isp-study') { const evidence = detail.snapshots.evidence[i]; if (evidence?.kind === 'isp_studies') await openTarget({kind: 'isp-study', review: detail, record: evidence.snapshot, isCurrent: () => s.detail?.id === detail.id}); }
          if (action === 'open-plan') { const plan = detail.next_experiments[i]; if (plan) await openTarget({kind: 'plan', review: detail, plan, isCurrent: () => s.detail?.id === detail.id}); }
          return;
        }
        if (s.pending || s.saving) return;
        model.edit(d => {
          if (action === 'add-requirement') { let n = 1; while (d.requirements.some(r => r.key === 'req-' + n)) n++; d.requirements.push({key: 'req-' + n, description: '', test_id: null}); }
          if (action === 'remove-requirement') { const key = d.requirements[i].key; d.requirements.splice(i, 1); d.evidence.forEach(e => unlinkRequirement(e, key)); }
          if (action === 'add-evidence') { if (!s.evidenceChoice) throw Error('연결할 기록을 선택하세요.'); const kind = s.evidenceKind || 'runs'; if (!d.evidence.some(e => e.kind === kind && e.id === s.evidenceChoice)) d.evidence.push({kind, id: s.evidenceChoice, requirement_keys: [], ...(kind==='isp_studies'?{dataset_requirements:{},selection_id:null}:{})}); }
          if (action === 'remove-evidence') d.evidence.splice(i, 1);
          if (action === 'add-plan') d.next_experiments.push({question: '', reason: '', mode: 'measurement', conditions: {}, assembly_id: null, test_id: null});
          if (action === 'remove-plan') d.next_experiments.splice(i, 1);
          if (action === 'add-condition' || action === 'remove-condition') { const plan = d.next_experiments[i], rows = plan._condition_rows || rowsFor(plan); if (action === 'add-condition') rows.push({key: '', value: '', type: 'text', unit: ''}); else rows.splice(j, 1); persistConditions(plan, rows); }
        });
        s.error = ''; render();
      } catch (error) { s.error = error.message; if (state.view === 'overview') render(); else toast(error.message, true); }
    })();
  });
  function offerISPStudy(id) {
    sync();
    if (model.s.pending || model.s.saving) throw Error('먼저 진행 중인 사례 저장 결과를 확인하세요.');
    if (!model.s.draft) model.begin(model.s.detail);
    model.s.editing = true; model.s.evidenceKind = 'isp_studies'; model.s.evidenceChoice = id;
    model.s.notice = 'ISP 탐색을 선택했습니다. 자료별 요구사항과 판단 이유를 확인한 뒤 근거를 연결해 저장하세요.';
    model.notify();
  }
  return {sync, view, offerISPStudy};
};

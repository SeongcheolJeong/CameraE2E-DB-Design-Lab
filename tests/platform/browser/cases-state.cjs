'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
vm.runInThisContext(fs.readFileSync('src/camera_platform/static/cases.js', 'utf8'));
const clone = value => JSON.parse(JSON.stringify(value));
const defer = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
const tick = () => new Promise(resolve => setImmediate(resolve));
function memory() { const data = new Map(); return {getItem: key => data.get(key), setItem: (key, value) => data.set(key, value)}; }
function detail(id, fields = {}) {
  return {id, schema: 'camera_case_review_v1', case_id: id, revision: 1, request_id: 'request-' + id,
    previous_revision_id: null, name: id, question: 'Which design?', requirements: [], assembly_ids: [], evidence: [],
    decision: {status: 'deferred', rationale: 'More evidence needed'}, unresolved: [], next_experiments: [],
    snapshots: {assemblies: [], tests: [], evidence: []}, content_hash: 'frozen', created_at: '2026-09-20T00:00:00Z', ...fields};
}
const create = (args = {}) => CameraCaseState({storage: memory(), storageKey: 'qa', requestId: () => 'request-fixed', api: async () => { throw Error('unexpected API call'); }, ...args});

test('late detail or failure cannot replace a newer case; stale history cannot leak across cases', async () => {
  const a = defer(), b = defer(), history = defer();
  const m = create({api: path => path.endsWith('/history') ? history.promise : path.endsWith('/a') ? a.promise : b.promise});
  const first = m.select('a'), second = m.select('b');
  b.resolve(detail('b')); await second;
  a.reject(Error('old fetch failed')); await first;
  assert.equal(m.s.detail.id, 'b'); assert.equal(m.s.error, '');
  const historyLoad = m.loadHistory();
  await m.select(''); history.resolve([detail('b')]); await historyLoad;
  assert.equal(m.s.selected, ''); assert.deepEqual(m.s.history, []);
});

test('detail identity rejection is visible and retry recovers; projections are never full evidence', async () => {
  let calls = 0;
  const m = create({api: async () => ++calls === 1 ? detail('wrong') : detail('a')});
  await m.select('a'); assert.equal(m.s.detail, null); assert.match(m.s.error, /일치/);
  await m.select('a'); assert.equal(m.s.detail.id, 'a'); assert.equal(m.s.error, '');
  const p = create({api: async () => ({...detail('a'), _projection: {}})});
  await p.select('a'); assert.equal(p.s.detail, null);
});

test('unsaved revision draft survives selection, navigation state, and reload without becoming the selected case', async () => {
  const storage = memory(), m = create({storage, api: async path => detail(path.split('/').at(-1))});
  await m.select('a'); m.begin(m.s.detail); m.edit(d => { d.question = 'Unsaved question'; });
  m.s.editing = false; m.notify(); await m.select('b');
  const restored = create({storage});
  assert.equal(restored.s.selected, 'b'); assert.equal(restored.s.draft.previous_revision_id, 'a');
  assert.equal(restored.s.draft.question, 'Unsaved question'); assert.equal(restored.s.editing, false);
  assert.equal(restored.begin(), false); assert.equal(restored.s.draft.question, 'Unsaved question');
});

test('lost POST response persists exact payload/request identity across reload; pending fields cannot change', async () => {
  const storage = memory(), sent = [];
  const m = create({storage, api: async (path, body) => { sent.push(clone(body)); throw Error('connection lost'); }});
  m.begin(); m.edit(d => { d.name = 'Original'; d.question = 'Why?'; d.decision.rationale = 'Uncertain'; });
  await m.save(); assert.ok(m.s.pending); assert.equal(m.discard(), false);
  assert.equal(m.edit(d => { d.name = 'Changed'; }), false);
  const restored = create({storage, requestId: () => { throw Error('must reuse original identity'); }, api: async (path, body) => { sent.push(clone(body)); return detail('saved', body); }});
  const saved = await restored.save();
  assert.equal(saved.id, 'saved'); assert.deepEqual(sent[0], sent[1]);
  assert.equal(restored.s.pending, null); assert.equal(restored.s.draft, null);
});

test('definitive validation error retains editable draft and corrected submission uses a new identity', async () => {
  let n = 0; const sent = [];
  const m = create({requestId: () => 'request-' + ++n, api: async (path, body) => {
    sent.push(clone(body)); if (sent.length === 1) throw Object.assign(Error('test mismatch'), {status: 422});
    return detail('saved', body);
  }});
  m.begin(); await m.save();
  assert.equal(m.s.pending, null); assert.ok(m.s.draft); assert.match(m.s.error, /test mismatch/);
  m.edit(d => { d.question = 'Corrected'; }); await m.save();
  assert.notEqual(sent[0].request_id, sent[1].request_id); assert.equal(sent[1].question, 'Corrected');
});

test('403 after an uncertain accepted POST cannot clear its identity or permit a duplicate save', async () => {
  const sent = []; let committed;
  const m = create({api: async (path, body) => {
    sent.push(clone(body));
    if (sent.length === 1) { committed = detail('one-case', body); throw Error('accepted but response lost'); }
    if (sent.length === 2) throw Object.assign(Error('token rotated'), {status: 403});
    return committed;
  }});
  m.begin(); await m.save(); await m.save();
  assert.ok(m.s.pending); assert.equal(m.edit(d => { d.name = 'Duplicate'; }), false);
  await m.save(); assert.equal(m.s.detail.id, 'one-case');
  assert.deepEqual(sent[0], sent[1]); assert.deepEqual(sent[1], sent[2]);
});

test('plan inputs accept explicit consistent units; missing/conflicting quantities never inherit earlier values', () => {
  assert.deepEqual(CameraCasePlanInputs({conditions: {exposure_s: {value: .004, unit: 's'}, seed: 7}}), {exposure: 4, seed: 7});
  for (const conditions of [{}, {exposure: 4}, {exposure_ms: {value: 4, unit: 's'}}, {exposure: {value: 4, unit: 'ms'}, exposure_s: {value: .008, unit: 's'}}, {exposure: {value: 4, unit: 'ms'}, exposure_ms: 'unknown'}]) {
    assert.deepEqual(CameraCasePlanInputs({conditions}), {exposure: null, seed: null});
  }
});

test('saving case A while selecting B cannot replace B on a late successful POST', async () => {
  const post = defer();
  const m = create({api: (path, body) => body ? post.promise : Promise.resolve(detail('b'))});
  m.begin(); const saving = m.save(); await m.select('b');
  post.resolve(detail('a', {request_id: 'request-fixed'})); await saving;
  assert.equal(m.s.selected, 'b'); assert.equal(m.s.detail.id, 'b'); assert.equal(m.s.draft, null);
});

test('storage failure is visible while in-memory draft remains usable', () => {
  const m = create({storage: {getItem: () => null, setItem: () => { throw Error('quota'); }}});
  m.begin(); m.edit(d => { d.question = 'Retained'; });
  assert.equal(m.s.draft.question, 'Retained'); assert.match(m.s.persistenceError, /보존/);
});

// Event contract tests execute the actual UI handlers and generated HTML without
// a browser. They do not claim rendered interaction or physical simulation.
function uiHarness(data, api) {
  const handlers = {}, storage = memory();
  globalThis.document = {addEventListener: (type, fn) => { (handlers[type] ||= []).push(fn); }};
  globalThis.sessionStorage = storage;
  const state = {data, view: 'overview'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const ui = CameraCaseUI({api, esc, records: key => state.data[key] || [], state, render() {}, refresh: async () => {}, toast() {}, downloadObject() {}, openTarget() {}});
  ui.sync();
  function emit(type, dataset, value = '', checked = false) {
    const target = {dataset, value, checked, disabled: false, id: type === 'submit' ? 'case-review-form' : '', closest: () => target, matches: () => Boolean(dataset.caseSelect)};
    for (const fn of handlers[type] || []) fn({target, preventDefault() {}});
  }
  return {ui, state, storage, click: (action, rest = {}) => emit('click', {caseAction: action, ...rest}), input: (field, value, rest = {}, checked = false) => emit('input', {caseField: field, ...rest}, value, checked), submit: () => emit('submit', {})};
}

test('empty case view ignores unrelated runs/PASS, studies remain selectable, and clearing unresolved is valid', async () => {
  const sent = [];
  const h = uiHarness({workspace: 'isolated', case_reviews: [], runs: [detail('unrelated', {status: 'succeeded'})], validations: [{id: 'global-pass', status: 'PASS'}], studies: [{id: 'study-done', name: 'Completed study', specification: {test_id: 't'}, run_ids: ['r']}], assemblies: [], tests: []}, async (path, body) => { sent.push(clone(body)); return detail('saved', body); });
  assert.match(h.ui.view(), /저장된 설계 사례가 없습니다/); assert.doesNotMatch(h.ui.view(), /global-pass|Lens–Sensor color shading/);
  h.click('new'); h.input('name', 'Question case'); h.input('question', 'Why?'); h.input('decision-rationale', 'Need evidence');
  h.input('unresolved', 'First\n\n'); h.input('evidence-kind', 'studies');
  assert.match(h.ui.view(), /Completed study/);
  h.input('unresolved', ''); h.submit(); await tick();
  assert.deepEqual(sent[0].unresolved, []); assert.deepEqual(sent[0].evidence, []);
});

test('condition rows survive refresh as a draft and submit typed values without editor fields', async () => {
  const sent = [];
  const h = uiHarness({workspace: 'conditions', case_reviews: []}, async (path, body) => { sent.push(clone(body)); return detail('saved', body); });
  h.click('new'); h.input('name', 'Typed conditions'); h.input('question', 'Exposure effect'); h.input('decision-rationale', 'Planned');
  h.click('add-plan'); h.input('plan-question', 'What changes?', {index: '0'}); h.input('plan-reason', 'Resolve gap', {index: '0'});
  h.click('add-condition', {index: '0'}); h.input('condition-key', 'exposure', {index: '0', sub: '0'}); h.input('condition-type', 'number', {index: '0', sub: '0'}); h.input('condition-value', '4', {index: '0', sub: '0'}); h.input('condition-unit', 'ms', {index: '0', sub: '0'});
  assert.match(h.ui.view(), /value="4"/);
  const savedDraft = JSON.parse(h.storage.getItem('camera-case-v1::conditions'));
  assert.equal(savedDraft.draft.next_experiments[0]._condition_rows[0].value, '4');
  h.submit(); await tick();
  assert.deepEqual(sent[0].next_experiments[0].conditions, {exposure: {value: 4, unit: 'ms'}});
  assert.equal('_condition_rows' in sent[0].next_experiments[0], false);
});

test('case overview shows only its linked evidence and identifies simulated observations', async () => {
  const measurement = {id: 'm', name: '<simulated capture>', source_kind: 'simulated'};
  const ref = {kind: 'measurements', id: 'm', requirement_keys: []};
  const value = detail('specific-case', {evidence: [ref], snapshots: {assemblies: [], tests: [], evidence: [{...ref, record_sha256: 'abc', snapshot: measurement, linked_records: []}]}});
  const h = uiHarness({workspace: 'scope', case_reviews: [value], measurements: [measurement, {id: 'other'}], runs: [{id: 'other-run', status: 'succeeded'}], validations: [{id: 'unrelated-pass', status: 'PASS'}]}, async () => value);
  await tick();
  const html = h.ui.view();
  assert.match(html, /0 계산 · 1 관측 데이터 · 0 비교 검증 · 0 설계 실험/);
  assert.match(html, /계산으로 생성한 관측/); assert.match(html, /&lt;simulated capture&gt;/);
  assert.doesNotMatch(html, /1 실측 데이터|unrelated-pass|other-run/);
});

test('unavailable case invalidates cached detail while preserving other case choices and drafts', async () => {
  const good = detail('healthy', {name: 'Healthy case'});
  const previous = detail('damaged', {name: 'Missing RAW case'});
  let missing = false;
  const h = uiHarness({workspace: 'availability', case_reviews: [previous, good]}, async path => {
    if (path.endsWith('/healthy')) return good;
    if (missing) throw Object.assign(Error('Original RAW is missing'), {status: 409});
    return previous;
  });
  await tick();
  assert.match(h.ui.view(), /data-case-review="damaged"/);
  h.click('new'); h.input('name', 'Retained draft');
  missing = true;
  h.state.data.case_reviews = [{schema: 'camera_case_review_unavailable_v1', id: 'damaged',
    name: previous.name, revision: 1, availability: {status: 'unavailable', reason: 'Original RAW is missing'}}, good];
  h.ui.sync(); await tick();
  const html = h.ui.view();
  assert.match(html, /근거 확인 불가/);
  assert.match(html, /Original RAW is missing/);
  assert.match(html, /Healthy case/);
  assert.match(html, /Retained draft/);
  assert.doesNotMatch(html, /data-case-review="damaged"|data-case-counts/);
  h.click('select', {id: 'healthy'}); await tick();
  assert.match(h.ui.view(), /data-case-review="healthy"/);
});

function ispCaseData(workspace) {
  return {workspace,case_reviews:[],tests:[{id:'test-flat',name:'Flat acquisition'},{id:'test-edge',name:'Edge acquisition'},{id:'other-test',name:'Different acquisition'}],
    isp_studies:[{id:'isp-study',name:'Fixed RAW ISP study',specification:{phase:'tuning'},datasets:[{key:'flat',test_id:'test-flat',assembly_id:'A'},{key:'edge',test_id:'test-edge',assembly_id:'A'}]}],
    isp_selections:[{id:'own-selection',study_id:'isp-study',name:'Own explicit choice'},{id:'other-selection',study_id:'other-study',name:'Unrelated choice'}]};
}
function addISPRequirements(h) {
  for(const [i,id] of ['test-flat','test-edge'].entries()) {h.click('add-requirement');h.input('requirement-description','Requirement '+i,{index:String(i)});h.input('requirement-test',id,{index:String(i)});}
  h.ui.offerISPStudy('isp-study');h.click('add-evidence');
  h.input('isp-dataset-requirement','req-1',{index:'0',dataset:'flat'},true);
  h.input('isp-dataset-requirement','req-2',{index:'0',dataset:'edge'},true);
}

test('ISP evidence maps each dataset to its frozen test and exports the explicit selection independently',async()=>{
  const sent=[],h=uiHarness(ispCaseData('isp-mapping'),async(path,body)=>{sent.push(clone(body));return detail('saved',body);});
  h.click('new');addISPRequirements(h);h.input('isp-selection','own-selection',{index:'0'});
  const html=h.ui.view();
  assert.match(html,/id="case-isp-link-0-flat-req-1"[^>]*checked[^>]*>/);
  assert.match(html,/id="case-isp-link-0-flat-req-2"[^>]*disabled/);
  assert.match(html,/id="case-isp-link-0-edge-req-1"[^>]*disabled/);
  assert.match(html,/id="case-isp-link-0-edge-req-2"[^>]*checked/);
  assert.match(html,/Own explicit choice/);assert.doesNotMatch(html,/Unrelated choice/);
  h.submit();await tick();assert.deepEqual(sent[0].evidence,[{kind:'isp_studies',id:'isp-study',requirement_keys:['req-1','req-2'],dataset_requirements:{flat:['req-1'],edge:['req-2']},selection_id:'own-selection'}]);
});

test('changing or deleting a requirement clears ISP dataset links as well as the flattened requirement union',async()=>{
  const h=uiHarness(ispCaseData('isp-cleanup'),async(path,body)=>detail('saved',body));h.click('new');addISPRequirements(h);
  h.input('requirement-test','other-test',{index:'0'});
  let draft=JSON.parse(h.storage.getItem('camera-case-v1::isp-cleanup')).draft;
  assert.deepEqual(draft.evidence[0].dataset_requirements,{edge:['req-2']});assert.deepEqual(draft.evidence[0].requirement_keys,['req-2']);
  assert.match(h.ui.view(),/id="case-isp-link-0-flat-req-1"[^>]*disabled/);
  h.click('remove-requirement',{index:'1'});draft=JSON.parse(h.storage.getItem('camera-case-v1::isp-cleanup')).draft;
  assert.deepEqual(draft.evidence[0].dataset_requirements,{});assert.deepEqual(draft.evidence[0].requirement_keys,[]);
  h.submit();await tick();assert.match(h.ui.view(),/각 근거가 확인하는 요구사항을 하나 이상/);
});

test('offering an ISP Study preserves the current unsaved draft and requires explicit evidence attachment',async()=>{
  const h=uiHarness(ispCaseData('isp-offer'),async()=>{throw Error('no request expected');});h.click('new');h.input('name','Existing case draft');h.input('question','Original question');h.input('decision-rationale','Need more evidence');h.click('add-plan');h.input('plan-question','Existing next experiment',{index:'0'});
  const before=JSON.parse(h.storage.getItem('camera-case-v1::isp-offer')).draft;h.click('hide-draft');h.ui.offerISPStudy('isp-study');
  const after=JSON.parse(h.storage.getItem('camera-case-v1::isp-offer'));assert.deepEqual(after.draft,before);assert.equal(after.editing,true);assert.equal(after.draft.evidence.length,0);assert.match(h.ui.view(),/Existing case draft/);assert.match(h.ui.view(),/Existing next experiment/);
  h.click('add-evidence');h.click('add-evidence');const attached=JSON.parse(h.storage.getItem('camera-case-v1::isp-offer')).draft;assert.equal(attached.evidence.length,1);assert.equal(attached.evidence[0].id,'isp-study');assert.deepEqual(attached.evidence[0].dataset_requirements,{});
});

test('an uncertain case save prevents offering a different ISP Study and retains its exact pending payload',async()=>{
  const h=uiHarness(ispCaseData('isp-pending'),async()=>{throw Error('response lost');});h.click('new');h.input('name','Pending original');h.submit();await tick();
  const before=JSON.parse(h.storage.getItem('camera-case-v1::isp-pending'));assert.ok(before.pending);
  assert.throws(()=>h.ui.offerISPStudy('isp-study'),/진행 중인 사례 저장/);
  const after=JSON.parse(h.storage.getItem('camera-case-v1::isp-pending'));assert.deepEqual(after.pending,before.pending);assert.deepEqual(after.draft,before.draft);
});

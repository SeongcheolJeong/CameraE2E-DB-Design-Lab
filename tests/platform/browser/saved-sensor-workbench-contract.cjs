'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const {
  SensorWorkbenchQualitySpecification,
  SensorWorkbenchSearchSpecification,
  SensorWorkbenchSummary,
} = require('../../../src/camera_platform/static/saved-sensor-workbench.js');

const qualityTemplate = {
  schema: 'camerae2e_saved_sensor_quality_study_v1', name: 'default', sensor_revisions: [],
  scenes: ['uniform'], luminance_cd_m2: 1000, exposure_s: .004, frames: 2,
  output_size: [32, 32], seed: 7, acknowledge_simulation_limits: false,
};
const searchTemplate = {
  schema: 'camerae2e_saved_sensor_search_study_v1', name: 'default',
  sensor_revision: {identifier: '', run_id: ''}, exposure_s: [.001, .004], analog_gain: [1, 2],
  luminance_cd_m2: 1000, sensor_size: [8, 8], max_saturation_fraction: 0,
  seed: 7, acknowledge_simulation_limits: false,
};
const catalog = [
  {report_code: 'DEP-1', run_id: 'run-a', status: 'simulated'},
  {report_code: 'DEF-2', run_id: 'run-b', status: 'simulated'},
  {report_code: 'OLD-3', run_id: 'run-c', status: 'failed'},
];
const qualityInput = {
  name: ' chart ', sensorA: 'run-a', sensorB: 'run-b',
  scenes: ['uniform', 'hdr_chart_d65'], luminance: '1000', exposureMs: '4',
  frames: '2', rows: '32', cols: '32', seed: '7', acknowledge: true,
};
const searchInput = {
  name: ' search ', sensor: 'run-a', exposuresMs: '1,4', gains: '1,2',
  luminance: '1000', ceiling: '0', rows: '8', cols: '8', seed: '7', acknowledge: true,
};

test('quality form binds pinned source, HDR chart size and original template immutably', () => {
  const spec = SensorWorkbenchQualitySpecification(qualityTemplate, catalog, qualityInput);
  assert.equal(spec.name, 'chart');
  assert.deepEqual(spec.sensor_revisions, [
    {identifier: 'DEP-1', run_id: 'run-a'}, {identifier: 'DEF-2', run_id: 'run-b'},
  ]);
  assert.deepEqual(spec.scenes, ['uniform', 'hdr_chart_d65']);
  assert.equal(spec.exposure_s, .004);
  assert.deepEqual(spec.output_size, [32, 32]);
  assert.equal(qualityTemplate.acknowledge_simulation_limits, false);
});

test('quality form refuses unsupported or duplicate run, extra scene, small HDR and missing acknowledgement', () => {
  for (const changed of [
    {sensorB: 'run-a'}, {sensorA: 'run-c'},
    {scenes: ['uniform', 'slanted_edge', 'macbeth_d65', 'hdr_chart_d65']},
    {rows: '31'}, {acknowledge: false}, {frames: '5'}, {exposureMs: ''},
  ]) assert.throws(() => SensorWorkbenchQualitySpecification(
    qualityTemplate, catalog, {...qualityInput, ...changed},
  ));
});

test('search form uses bounded declared grid and electron objective server contract', () => {
  const spec = SensorWorkbenchSearchSpecification(searchTemplate, catalog, searchInput);
  assert.equal(spec.name, 'search');
  assert.deepEqual(spec.sensor_revision, {identifier: 'DEP-1', run_id: 'run-a'});
  assert.deepEqual(spec.exposure_s, [.001, .004]);
  assert.deepEqual(spec.analog_gain, [1, 2]);
  assert.deepEqual(spec.sensor_size, [8, 8]);
  assert.equal(spec.max_saturation_fraction, 0);
  assert.equal(searchTemplate.acknowledge_simulation_limits, false);
});

test('search form refuses unsupported run, excess grid, duplicates and invalid limits', () => {
  for (const changed of [
    {sensor: 'run-c'}, {exposuresMs: '1,2,4'}, {gains: '1,1'},
    {exposuresMs: '1,,4'}, {ceiling: '1.1'}, {rows: '7'},
    {acknowledge: false}, {seed: ''},
  ]) assert.throws(() => SensorWorkbenchSearchSpecification(
    searchTemplate, catalog, {...searchInput, ...changed},
  ));
});

test('result display gates on completed typed record and supported report contract', () => {
  const quality = {job_kind: 'saved_sensor_quality', status: 'succeeded', result: {summary: {
    schema: qualityTemplate.schema, status: 'completed', sensor_reports: [{cases: []}],
  }}};
  const search = {job_kind: 'saved_sensor_search', status: 'succeeded', result: {summary: {
    schema: searchTemplate.schema, status: 'completed', search: {
      schema: 'camerae2e_saved_sensor_search_v1', case_count: 1, cases: [{}],
    },
  }}};
  assert.equal(SensorWorkbenchSummary('quality', quality), quality.result.summary);
  assert.equal(SensorWorkbenchSummary('search', search), search.result.summary);
  assert.equal(SensorWorkbenchSummary('quality', {...quality, status: 'failed'}), null);
  assert.throws(() => SensorWorkbenchSummary('search', {...search, job_kind: 'saved_sensor_quality'}));
  assert.throws(() => SensorWorkbenchSummary('search', {
    ...search, result: {summary: {...search.result.summary, search: {...search.result.summary.search, case_count: 2}}},
  }));
});

test('page exposes saved-run scope, stored artifacts and no HTML string injection path', () => {
  const html = fs.readFileSync('src/camera_platform/static/saved-sensor-workbench.html', 'utf8');
  const script = fs.readFileSync('src/camera_platform/static/saved-sensor-workbench.js', 'utf8');
  assert.match(html, /HDR fusion/);
  assert.match(html, /저장 센서 run/);
  assert.match(html, /저장된 산출물/);
  assert.match(script, /hdr_chart_response/);
  assert.match(script, /mean_electrons_e_per_pixel/);
  assert.match(script, /textContent/);
  assert.doesNotMatch(script, /innerHTML/);
});

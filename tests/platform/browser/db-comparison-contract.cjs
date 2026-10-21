'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const {CameraDbComparisonSpecification, CameraDbComparisonTracks} = require('../../../src/camera_platform/static/db-comparisons.js');

const template = {
  schema: 'camera_db_comparison_v1', name: 'default', lens_revision_ids: [], sensor_revisions: [],
  wavelength_nm: 550, scene: {type: 'uniform ee', args: [[8, 8]]}, sensor_size: [8, 8],
  exposure_s: .004, noise_flag: 0, seed: 7, readout_overrides: {},
  acknowledge_simulation_limits: false,
};
const lenses = {revisions: [
  {revision_id: 'lens-a', preview_status: 'ready'},
  {revision_id: 'lens-b', preview_status: 'ready'},
  {revision_id: 'lens-old', preview_status: 'blocked'},
]};
const sensors = [
  {report_code: 'A', run_id: 'run-a', status: 'simulated'},
  {report_code: 'B', run_id: 'run-b', status: 'simulated'},
  {report_code: 'C', run_id: 'run-c', status: 'failed'},
];
const input = {
  name: '  DB comparison  ', lensA: 'lens-a', lensB: 'lens-b', sensorA: 'run-a', sensorB: 'run-b',
  wavelength: '550', exposureMs: '4', height: '8', width: '8', noise: '0', seed: '7',
  readoutOverrides: {gain: '2', bit_depth: '', read_noise_e: ''}, acknowledge: true,
};

test('form binds exactly two ready lenses and two pinned executable sensor runs', () => {
  const spec = CameraDbComparisonSpecification(template, lenses, sensors, input);
  assert.deepEqual(spec.lens_revision_ids, ['lens-a', 'lens-b']);
  assert.deepEqual(spec.sensor_revisions, [
    {identifier: 'A', run_id: 'run-a'}, {identifier: 'B', run_id: 'run-b'},
  ]);
  assert.deepEqual(spec.scene, {type: 'uniform ee', args: [[8, 8]]});
  assert.deepEqual(spec.sensor_size, [8, 8]);
  assert.equal(spec.exposure_s, .004);
  assert.deepEqual(spec.readout_overrides, {gain: 2});
  assert.equal(spec.name, 'DB comparison');
  assert.equal(template.acknowledge_simulation_limits, false);
});

test('form refuses same or unsupported revision, missing acknowledgement and invalid scene size', () => {
  for (const changed of [
    {lensB: 'lens-a'}, {lensB: 'lens-old'}, {sensorB: 'run-a'}, {sensorB: 'run-c'},
    {acknowledge: false}, {height: '7'}, {readoutOverrides: {bit_depth: '12.5'}},
  ]) assert.throws(() => CameraDbComparisonSpecification(template, lenses, sensors, {...input, ...changed}));
});

test('result is visible only when both separate tracks and not-executed cross product are saved', () => {
  const summary = {schema: 'camera_db_comparison_v1', status: 'completed',
    lens_geometry: {lenses: [{}, {}]}, sensor_raw: {captures: [{}, {}]},
    lens_sensor_cross_product: {status: 'not_executed'}};
  assert.equal(CameraDbComparisonTracks({status: 'succeeded', result: {summary}}), summary);
  assert.equal(CameraDbComparisonTracks({status: 'failed', result: {summary}}), null);
  assert.throws(() => CameraDbComparisonTracks({status: 'succeeded', result: {summary: {
    ...summary, lens_sensor_cross_product: {status: 'completed'},
  }}}), /별도 트랙/);
});

test('page describes the two tracks and the physical limit', () => {
  const html = fs.readFileSync('src/camera_platform/static/db-comparisons.html', 'utf8');
  const script = fs.readFileSync('src/camera_platform/static/db-comparisons.js', 'utf8');
  assert.match(html, /렌즈×센서 2×2 RAW 결합은 실행하지 않습니다/);
  assert.match(html, /렌즈 · geometric spot/);
  assert.match(html, /센서 · 별도 RAW/);
  assert.match(html, /acknowledge/);
  assert.match(script, /saved_readout/);
  assert.match(script, /공통 override/);
  assert.match(script, /effective_readout/);
});

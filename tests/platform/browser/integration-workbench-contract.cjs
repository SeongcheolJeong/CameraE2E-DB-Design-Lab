'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {
  INTEGRATION, EDITABLE_KINDS, integrationAnnotationDocument, integrationSelectableLenses,
  integrationSpec, integrationSummary,
} = require('../../../src/camera_platform/static/integration-workbench.js');

const lensTemplate = {
  schema: INTEGRATION.lens.schema, name: 'preview', lens_revision_id: '',
  sensor_revision: {identifier: '', run_id: ''}, scene_kind: 'slanted_edge',
  sensor_size: [8, 8], luminance_cd_m2: 1000, exposure_s: .004,
  noise_flag: 0, seed: 7, accept_approximate_lens_radiometry: false,
};
const catalogs = {
  lenses: [{revision_id: 'lens-rev', preview_status: 'ready'},
    {revision_id: 'blocked-rev', preview_status: 'blocked'}],
  sensors: [{run_id: 'sensor-run', report_code: 'DEP-2209-801', status: 'simulated', model: 'db_estimate_v1'}],
  hdrLenses: [{revision_id: 'lens-rev', preview_status: 'ready'}],
  hdrSensors: [{run_id: 'sensor-run', report_code: 'DEP-2209-801', status: 'simulated', model: 'db_estimate_v1'}],
  lfmLenses: [{revision_id: 'lens-rev', preview_status: 'ready'}],
  lfmSensors: [{run_id: 'sensor-run', report_code: 'DEP-2209-801', status: 'simulated', model: 'db_estimate_v1'}],
  pwmHdrLfmJobs: [{id: 'lfm-1', name: 'saved PWM', status: 'succeeded'}],
  qualityJobs: [{id: 'quality-1', status: 'succeeded'}],
  ispLensJobs: [{id: 'lens-1', status: 'succeeded'}],
  cohortJobs: [{id: 'lens-1', status: 'succeeded'}],
  datasetLensJobs: [{id: 'lens-1', status: 'succeeded'}],
  adaptiveRawJobs: [{id: 'lens-1', status: 'succeeded'}],
  detectorIspJobs: [{id: 'isp-1', status: 'succeeded'}],
  jointQualityJobs: ['L1S1', 'L1S2', 'L2S1', 'L2S2'].map((slot, index) => ({
    id: `matrix-${slot}`, name: slot, status: 'succeeded',
    lens_revision_id: index < 2 ? 'lens-a' : 'lens-b',
    sensor_run_id: index % 2 ? 'sensor-b' : 'sensor-a',
    horizontal_fov_deg: .01 + index * .001,
  })),
};
assert.deepEqual(integrationSelectableLenses(catalogs.lenses).map(row => row.revision_id), ['lens-rev']);
assert.deepEqual(integrationSelectableLenses(catalogs.lenses, 'blocked', true)
  .map(row => row.revision_id), ['blocked-rev']);
assert.deepEqual(integrationSelectableLenses(catalogs.lenses, 'blocked'), []);
const lensInput = {name: ' Lens test ', lens: 'lens-rev', sensor: 'sensor-run', scene: 'slanted_edge',
  rows: '8', cols: '8', luminance: '1000', exposureMs: '4', noise: '0', seed: '7', ack: true};
const lens = integrationSpec('lens', lensTemplate, catalogs, lensInput);
assert.equal(lens.name, 'Lens test');
assert.equal(lens.lens_revision_id, 'lens-rev');
assert.deepEqual(lens.sensor_revision, {identifier: 'DEP-2209-801', run_id: 'sensor-run'});
assert.equal(lens.exposure_s, .004);
assert.equal(lens.accept_approximate_lens_radiometry, true);
assert.throws(() => integrationSpec('lens', lensTemplate, catalogs, {...lensInput, ack: false}), /범위/);
assert.throws(() => integrationSpec('lens', lensTemplate, catalogs, {...lensInput, lens: 'blocked-rev'}), /PSF/);
const localLensTemplate = {...lensTemplate, schema: 'camerae2e_lens_sensor_raw_preview_study_v2',
  field_psf_index: 0, field_azimuth_deg: 0};
const boundedLensTemplate = {...lensTemplate, schema: 'camerae2e_lens_sensor_raw_preview_study_v3'};
const commonSceneLensTemplate = {...lensTemplate,
  schema: 'camerae2e_lens_sensor_raw_preview_study_v4', scene_horizontal_fov_deg: .2};
const boundedLens = integrationSpec('lens', boundedLensTemplate, catalogs,
  {...lensInput, scope: 'central_bounded'});
assert.equal(boundedLens.schema, boundedLensTemplate.schema);
const commonSceneLens = integrationSpec('lens', commonSceneLensTemplate, catalogs,
  {...lensInput, scope: 'central_bounded_common_scene', sceneHorizontalFovDeg: '.25'});
assert.equal(commonSceneLens.scene_horizontal_fov_deg, .25);
const denseSceneLensTemplate = {...commonSceneLensTemplate,
  schema: 'camerae2e_lens_sensor_raw_preview_study_v5', scene_samples_row_col: [128, 128]};
const denseSceneLens = integrationSpec('lens', denseSceneLensTemplate, catalogs,
  {...lensInput, scope: 'central_bounded_dense_common_scene', sceneHorizontalFovDeg: '.25',
    sceneSamplesRows: '128', sceneSamplesCols: '128'});
assert.deepEqual(denseSceneLens.scene_samples_row_col, [128, 128]);
assert.equal(denseSceneLens.scene_horizontal_fov_deg, .25);
assert.throws(() => integrationSpec('lens', denseSceneLensTemplate, catalogs,
  {...lensInput, scope: 'central_bounded_dense_common_scene', sceneHorizontalFovDeg: '.25',
    sceneSamplesRows: '128', sceneSamplesCols: '257'}), /표본 열/);
assert.throws(() => integrationSpec('lens', commonSceneLensTemplate, catalogs,
  {...lensInput, scope: 'central_bounded_common_scene', sceneHorizontalFovDeg: '31'}), /화각/);
assert.throws(() => integrationSpec('lens', boundedLensTemplate, catalogs,
  {...lensInput, scope: 'central'}), /시야 범위/);
const localLens = integrationSpec('lens', localLensTemplate, catalogs,
  {...lensInput, scope: 'local_field_psf', fieldIndex: '2', fieldAzimuth: '90'});
assert.equal(localLens.field_psf_index, 2);
assert.equal(localLens.field_azimuth_deg, 90);
assert.throws(() => integrationSpec('lens', localLensTemplate, catalogs,
  {...lensInput, scope: 'central', fieldIndex: '2', fieldAzimuth: '90'}), /시야 범위/);

const hdrTemplate = {schema: INTEGRATION.hdr.schema, name: 'HDR', lens_revision_id: '',
  sensor_revision: {identifier: '', run_id: ''}, exposure_s: [.001, .004],
  scene_kind: 'slanted_edge', sensor_size: [8, 8], luminance_cd_m2: 1000,
  seed: 7, accept_approximate_lens_radiometry: false};
const hdrInput = {name: 'HDR', lens: 'lens-rev', sensor: 'sensor-run', scene: 'slanted_edge',
  rows: '8', cols: '8', exposuresMs: '1, 4', luminance: '1000', seed: '7', ack: true};
assert.deepEqual(integrationSpec('hdr', hdrTemplate, catalogs, hdrInput).exposure_s, [.001, .004]);
assert.throws(() => integrationSpec('hdr', hdrTemplate, catalogs,
  {...hdrInput, exposuresMs: '4, 1'}), /오름차순/);
const boundedHdrTemplate = {...hdrTemplate, schema: 'camerae2e_lens_sensor_static_hdr_study_v2'};
assert.equal(integrationSpec('hdr', boundedHdrTemplate, catalogs,
  {...hdrInput, scope: 'central_bounded'}).schema, boundedHdrTemplate.schema);
assert.throws(() => integrationSpec('hdr', boundedHdrTemplate, catalogs,
  {...hdrInput, scope: 'central'}), /PSF 실행 범위/);

const lfmTemplate = {
  schema: INTEGRATION.lfm.schema, name: 'PWM', lens_revision_id: '',
  sensor_revision: {identifier: '', run_id: ''}, sensor_size: [8, 8],
  exposure_s: [.001, .004], pwm_period_s: .002, duty_cycle: .5,
  off_to_on_ratio: .02, on_luminance_cd_m2: 1000, seed: 7,
  accept_approximate_lens_radiometry: false, acknowledge_simulation_limits: false,
};
const lfmInput = {name: 'PWM', lens: 'lens-rev', sensor: 'sensor-run', rows: '8', cols: '8',
  exposuresMs: '1, 4', periodMs: '2', duty: '.5', offRatio: '.02', onLuminance: '1000',
  seed: '7', ack: true};
const lfm = integrationSpec('lfm', lfmTemplate, catalogs, lfmInput);
assert.deepEqual(lfm.exposure_s, [.001, .004]);
assert.equal(lfm.pwm_period_s, .002);
assert.throws(() => integrationSpec('lfm', lfmTemplate, catalogs,
  {...lfmInput, exposuresMs: '4, 1'}), /오름차순/);
assert.throws(() => integrationSpec('lfm', {...lfmTemplate, invented_field: 1}, catalogs, lfmInput), /계약/);
const boundedLfmTemplate = {...lfmTemplate, schema: 'camerae2e_lens_sensor_pwm_lfm_study_v2'};
const timedLfmTemplate = {...boundedLfmTemplate,
  schema: 'camerae2e_lens_sensor_pwm_lfm_study_v3', timing_mode_sha256: ''};
const pinnedTimingSha = 'b'.repeat(64);
assert.equal(integrationSpec('lfm', timedLfmTemplate, catalogs,
  {...lfmInput, scope: 'central_bounded_timed', timingModeSha256: pinnedTimingSha})
  .timing_mode_sha256, pinnedTimingSha);
assert.throws(() => integrationSpec('lfm', timedLfmTemplate, catalogs,
  {...lfmInput, scope: 'central_bounded_timed', timingModeSha256: ''}), /시간 모드 SHA-256/);
assert.equal(integrationSpec('lfm', boundedLfmTemplate, catalogs,
  {...lfmInput, scope: 'central_bounded'}).schema, boundedLfmTemplate.schema);
assert.throws(() => integrationSpec('lfm', boundedLfmTemplate, catalogs,
  {...lfmInput, scope: 'central'}), /PSF 실행 범위/);

const pwmHdrTemplate = {schema: INTEGRATION.pwmHdr.schema, name: 'PWM HDR',
  source_lfm_job_id: '', acknowledge_simulation_limits: false};
const pwmHdrInput = {name: 'PWM HDR', source: 'lfm-1', ack: true};
assert.equal(integrationSpec('pwmHdr', pwmHdrTemplate, catalogs, pwmHdrInput).source_lfm_job_id,
  'lfm-1');
assert.throws(() => integrationSpec('pwmHdr', pwmHdrTemplate, catalogs,
  {...pwmHdrInput, source: 'unknown'}), /완료된/);

const datasetTemplate = {schema: INTEGRATION.dataset.schema, name: 'dataset',
  source_quality_job_id: '', sensor_report_index: 0, case_index: 0,
  split: 'train', labels: 'unlabeled', acknowledge_simulation_limits: false};
const dataset = integrationSpec('dataset', datasetTemplate, catalogs,
  {name: 'retained', source: 'quality-1', report: '0', case: '1', split: 'train', ack: true});
assert.equal(dataset.source_quality_job_id, 'quality-1');
assert.equal(dataset.labels, 'unlabeled');
assert.equal(dataset.case_index, 1);
const lensDatasetTemplate = {schema: 'camerae2e_retained_raw_dataset_study_v2', name: 'lens dataset',
  source_kind: 'lens_sensor_raw', source_job_id: '', split: 'train', labels: 'unlabeled',
  acknowledge_simulation_limits: false};
const lensDataset = integrationSpec('dataset', lensDatasetTemplate, catalogs,
  {name: 'combined RAW', sourceKind: 'lens_sensor_raw', source: 'lens-1', split: 'test', ack: true});
assert.equal(lensDataset.source_job_id, 'lens-1');
assert.equal(lensDataset.split, 'test');
assert.throws(() => integrationSpec('dataset', lensDatasetTemplate, catalogs,
  {name: 'wrong', sourceKind: 'saved_sensor_quality', source: 'quality-1', split: 'train', ack: true}), /렌즈/);
const labeledDatasetTemplate = {...lensDatasetTemplate,
  schema: 'camerae2e_retained_raw_dataset_study_v3', name: 'labeled RAW',
  labels: 'user_provided_unverified', annotation_artifact_sha256: ''};
const annotationTemplate = {
  schema: 'camerae2e_retained_raw_annotations_v1',
  source_binding: {source_lens_sensor_job_id: 'lens-1', source_raw_content_sha256: 'sha256:fixture'},
  raw_shape: [8, 8], coordinate_frame: 'raw_mosaic_pixel_xyxy_half_open',
  provenance: {provider_id: '', annotation_set_id: '', method: 'human_review', coverage: 'full_frame_reviewed'},
  objects: [],
};
const labelInput = {source: 'lens-1', labelProvider: 'fixture-author', labelSet: 'fixture-v1',
  labelMethod: 'human_review', labelCoverage: 'partial',
  labelObjects: '[{"label":"object","bbox_xyxy":[1,1,5,5]}]'};
const annotation = integrationAnnotationDocument(annotationTemplate, labelInput);
assert.deepEqual(annotation.objects, [{label: 'object', bbox_xyxy: [1, 1, 5, 5]}]);
assert.equal(annotation.provenance.provider_id, 'fixture-author');
assert.throws(() => integrationAnnotationDocument(annotationTemplate,
  {...labelInput, labelObjects: '[{"label":"object","bbox_xyxy":[1,1,9,5]}]'}), /박스/);
const labeledDataset = integrationSpec('dataset', labeledDatasetTemplate, catalogs,
  {name: 'labeled', sourceKind: 'lens_sensor_raw_labeled', source: 'lens-1',
    split: 'train', annotationSha256: 'a'.repeat(64), ack: true});
assert.equal(labeledDataset.labels, 'user_provided_unverified');
assert.equal(labeledDataset.annotation_artifact_sha256, 'a'.repeat(64));

const ispProfile = {
  schema: 'camera_isp_profile_v2', calibration_id: null,
  spatial_lsc: {mode: 'bypass'}, raw_denoise: {mode: 'bypass'},
  white_balance: {gains_rgb: [1, 1, 1]},
  ccm: {matrix_rgb: [[1, 0, 0], [0, 1, 0], [0, 0, 1]]},
  tone: {curve: 'srgb'},
  linear_sharpen: {mode: 'bypass', amount: .25, threshold: .01, max_delta: .1},
};
const ispTemplate = {schema: INTEGRATION.isp.schema, name: 'RAW ISP',
  source_lens_sensor_job_id: '', profile: ispProfile, acknowledge_simulation_limits: false};
const ispInput = {name: 'ISP test', source: 'lens-1',
  wbR: '1.5', wbG: '1', wbB: '.8', ccm: '1,0,0, 0,1,0, 0,0,1', tone: 'linear',
  sharpen: true, sharpenAmount: '.4', ack: true};
const isp = integrationSpec('isp', ispTemplate, catalogs, ispInput);
assert.equal(isp.source_lens_sensor_job_id, 'lens-1');
assert.deepEqual(isp.profile.white_balance.gains_rgb, [1.5, 1, .8]);
assert.equal(isp.profile.linear_sharpen.mode, 'bounded_detail_v1');
assert.equal(isp.profile.linear_sharpen.amount, .4);
assert.equal(isp.profile.linear_sharpen.threshold, .01);
assert.equal(isp.profile.tone.curve, 'linear');
assert.equal(isp.profile.spatial_lsc.mode, 'bypass');
assert.equal(isp.profile.raw_denoise.mode, 'bypass');
assert.equal(ispTemplate.profile.white_balance.gains_rgb[0], 1);
assert.throws(() => integrationSpec('isp', {...ispTemplate,
  profile: {...ispProfile, spatial_lsc: {mode: 'native_four_site_grid_v1'}}}, catalogs, ispInput), /bypass/);
assert.throws(() => integrationSpec('isp', ispTemplate, catalogs, {...ispInput, source: 'unknown'}), /완료/);
assert.throws(() => integrationSpec('isp', ispTemplate, catalogs, {...ispInput, ccm: '1,2'}), /9개/);
assert.ok(EDITABLE_KINDS.includes('isp'));

const jointQualityTemplate = {schema: INTEGRATION.jointQuality.schema, name: '2×2',
  source_jobs: {L1S1: '', L1S2: '', L2S1: '', L2S2: ''}, profile: ispProfile,
  acknowledge_simulation_limits: false};
const matrixInput = {name: 'matrix', sourceJobs: Object.fromEntries(
  ['L1S1', 'L1S2', 'L2S1', 'L2S2'].map(slot => [slot, `matrix-${slot}`])), ack: true};
const matrix = integrationSpec('jointQuality', jointQualityTemplate, catalogs, matrixInput);
assert.deepEqual(matrix.source_jobs, matrixInput.sourceJobs);
assert.equal(matrix.acknowledge_simulation_limits, true);
assert.throws(() => integrationSpec('jointQuality', jointQualityTemplate, catalogs,
  {...matrixInput, sourceJobs: {...matrixInput.sourceJobs, L2S2: 'matrix-L1S1'}}), /서로 다른/);

const adaptiveTemplate = {schema: INTEGRATION.adaptive.schema, name: 'Adaptive',
  source_lens_sensor_job_id: '', method: 'adaptive_laplacian_v1',
  acknowledge_simulation_limits: false};
const adaptive = integrationSpec('adaptive', adaptiveTemplate, catalogs,
  {name: 'Adaptive', source: 'lens-1', ack: true});
assert.equal(adaptive.source_lens_sensor_job_id, 'lens-1');
assert.throws(() => integrationSpec('adaptive', adaptiveTemplate, catalogs,
  {name: 'Adaptive', source: 'missing', ack: true}), /완료 RAW/);
const detectorTemplate = {schema: INTEGRATION.detector.schema, name: 'Detector',
  source_lens_sensor_job_id: '', source_lens_sensor_isp_job_id: '',
  model_profile: 'ultralytics_yolo11n_detection', model_weights_sha256: 'b'.repeat(64),
  target_labels: ['person'], score_threshold: .25, annotation_artifact_sha256: '',
  iou_threshold: .5, acknowledge_simulation_limits: false};
const detector = integrationSpec('detector', detectorTemplate, catalogs,
  {name: 'Detector', raw: 'lens-1', isp: 'isp-1', labels: 'person', score: '.25', ack: true});
assert.equal(detector.source_lens_sensor_job_id, 'lens-1');
assert.equal(detector.source_lens_sensor_isp_job_id, 'isp-1');
assert.equal(detector.model_weights_sha256, 'b'.repeat(64));
assert.throws(() => integrationSpec('detector', {...detectorTemplate, model_weights_sha256: ''}, catalogs,
  {name: 'Detector', raw: 'lens-1', isp: 'isp-1', labels: 'person', score: '.25', ack: true}), /가중치/);

assert.equal(integrationSummary('lens', {job_kind: 'lens_sensor_raw', status: 'succeeded',
  result: {summary: {schema: INTEGRATION.lens.schema, status: 'completed'}}}).status, 'completed');
assert.throws(() => integrationSummary('lens', {job_kind: 'saved_sensor_hdr', status: 'succeeded'}), /종류/);
assert.equal(integrationSummary('isp', {job_kind: INTEGRATION.isp.kind, status: 'succeeded',
  result: {summary: {schema: INTEGRATION.isp.schema, status: 'completed'}}}).status, 'completed');
assert.equal(integrationSummary('adaptive', {job_kind: INTEGRATION.adaptive.kind, status: 'succeeded',
  result: {summary: {schema: INTEGRATION.adaptive.schema, status: 'completed'}}}).status, 'completed');
assert.equal(integrationSummary('detector', {job_kind: INTEGRATION.detector.kind, status: 'succeeded',
  result: {summary: {schema: INTEGRATION.detector.schema, status: 'completed'}}}).status, 'completed');
assert.equal(integrationSummary('dataset', {job_kind: INTEGRATION.dataset.kind, status: 'succeeded',
  result: {summary: {schema: lensDatasetTemplate.schema, status: 'completed'}}}).status, 'completed');
assert.equal(integrationSummary('hdr', {job_kind: INTEGRATION.hdr.kind, status: 'succeeded',
  result: {summary: {schema: boundedHdrTemplate.schema, status: 'completed'}}}).status, 'completed');
assert.equal(integrationSummary('lfm', {job_kind: INTEGRATION.lfm.kind, status: 'succeeded',
  result: {summary: {schema: boundedLfmTemplate.schema, status: 'completed'}}}).status, 'completed');
assert.equal(integrationSummary('pwmHdr', {job_kind: INTEGRATION.pwmHdr.kind,
  status: 'succeeded', result: {summary: {schema: pwmHdrTemplate.schema,
    status: 'inconclusive'}}}).status, 'inconclusive');

// Exercise the browser entry point too: missing ISP template hydration, source wiring,
// or submit registration must fail even if integrationSpec itself still works.
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.value = ''; this.checked = false; this.children = [];
    this.listeners = {}; this.hidden = false; this.disabled = false; this.textContent = '';
    this.dataset = {};
  }
  replaceChildren(...children) {
    this.children = children;
    this.value = children.length && children[0].value !== undefined ? children[0].value : '';
  }
  append(...children) { this.children.push(...children); }
  setAttribute() {}
  querySelectorAll() { return []; }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  async dispatch(type) {
    for (const callback of this.listeners[type] || []) await callback({preventDefault() {}});
  }
}
const elements = new Map();
const browserDocument = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  },
  createElement(tag) { return new Element(tag); },
};
const element = id => browserDocument.getElementById(id);
const requests = [];
const ispJobSummary = {
  schema: INTEGRATION.isp.schema, status: 'completed', source_binding: {source_lens_sensor_job_id: 'lens-1'},
  specification: isp, raw_format: {cfa: 'RGGB', bit_depth: 12}, raw_values_byte_equal: true,
  before_after: {valid_pixel_fraction: 1, default_vs_candidate: {available: true,
    mean_absolute_rgb_difference: .1, changed_sample_fraction: .8, quality_improvement_claimed: false}},
  candidate_rgb_consumer: {tone_curve: 'linear'},
  verification: {physical_validation: 'not_established'}, limitation: 'Diagnostic replay only.',
};
const responses = {
  '/api/v1/session': {schema: 'camera_platform_v1', token: 'test-token'},
  '/api/v1/lens-sensor-raw/template': {specification: lensTemplate, limitation: 'preview',
    lens_catalog: {revisions: catalogs.lenses}, sensor_catalog: catalogs.sensors},
  '/api/v1/lens-sensor-raw/template?scope=local_field_psf': {
    specification: localLensTemplate, limitation: 'Local PSF only',
    lens_catalog: {revisions: catalogs.lenses}, sensor_catalog: catalogs.sensors},
  '/api/v1/lens-sensor-raw/template?scope=central_bounded': {
    specification: boundedLensTemplate, limitation: 'Bounded strict PSF',
    lens_catalog: {revisions: catalogs.lenses}, sensor_catalog: catalogs.sensors},
  '/api/v1/lens-sensor-raw/template?scope=central_bounded_common_scene': {
    specification: commonSceneLensTemplate, limitation: 'Common input scene only',
    lens_catalog: {revisions: catalogs.lenses}, sensor_catalog: catalogs.sensors},
  '/api/v1/lens-sensor-raw/template?scope=central_bounded_dense_common_scene': {
    specification: denseSceneLensTemplate, limitation: 'Dense common input scene only',
    lens_catalog: {revisions: catalogs.lenses}, sensor_catalog: catalogs.sensors},
  '/api/v1/lens-sensor-hdr/template': {specification: hdrTemplate,
    limitation: 'HDR limits', lens_catalog: {revisions: catalogs.hdrLenses},
    sensor_catalog: catalogs.hdrSensors},
  '/api/v1/lens-sensor-hdr/template?scope=central_bounded': {specification: boundedHdrTemplate,
    limitation: 'Bounded HDR limits', lens_catalog: {revisions: catalogs.hdrLenses},
    sensor_catalog: catalogs.hdrSensors},
  '/api/v1/lens-sensor-lfm/template': {specification: lfmTemplate, limitation: 'LFM limits',
    lens_catalog: {revisions: catalogs.lfmLenses}, sensor_catalog: catalogs.lfmSensors},
  '/api/v1/lens-sensor-lfm/template?scope=central_bounded': {specification: boundedLfmTemplate,
    limitation: 'Bounded LFM limits', lens_catalog: {revisions: catalogs.lfmLenses},
    sensor_catalog: catalogs.lfmSensors},
  '/api/v1/lens-sensor-lfm/template?scope=central_bounded_timed': {specification: timedLfmTemplate,
    limitation: 'Pinned timing declaration only', lens_catalog: {revisions: catalogs.lfmLenses},
    sensor_catalog: catalogs.lfmSensors},
  '/api/v1/lens-sensor-pwm-hdr/template': {specification: pwmHdrTemplate,
    source_lfm_jobs: catalogs.pwmHdrLfmJobs, limitation: 'Authored timing only'},
  '/api/v1/lens-sensor-pwm-hdr/pwm-hdr-1': {id: 'pwm-hdr-1',
    job_kind: INTEGRATION.pwmHdr.kind, status: 'succeeded',
    result: {summary: {schema: pwmHdrTemplate.schema, status: 'inconclusive',
      source_lfm_job_id: 'lfm-1', fused_hdr_pixels: 4,
      single_bracket_fallback_pixels: 112, invalid_pixels: 140,
      report: {source_lfm_schema: boundedLfmTemplate.schema},
      limitation: 'Authored timing only'}}, artifacts: []},
  '/api/v1/lens-sensor-isp-replays/template': {specification: ispTemplate,
    source_lens_sensor_jobs: catalogs.ispLensJobs, limitation: 'ISP limits'},
  '/api/v1/retained-raw-datasets/template': {specification: datasetTemplate,
    source_quality_jobs: catalogs.qualityJobs, limitation: 'Dataset limits'},
  '/api/v1/retained-raw-datasets/template?source_kind=lens_sensor_raw': {
    specification: lensDatasetTemplate, source_jobs: catalogs.datasetLensJobs,
    limitation: 'Lens dataset limits'},
  '/api/v1/retained-raw-datasets/template?source_kind=lens_sensor_raw_labeled': {
    specification: labeledDatasetTemplate, source_jobs: catalogs.datasetLensJobs,
    limitation: 'User labels are unverified'},
  '/api/v1/retained-raw-datasets/template?source_kind=lens_sensor_raw_labeled&source_job_id=lens-1': {
    specification: labeledDatasetTemplate, source_jobs: catalogs.datasetLensJobs,
    annotation_template: annotationTemplate, limitation: 'User labels are unverified'},
  '/api/v1/lens-sensor-raw-cohorts/template': {
    specification: {schema: INTEGRATION.cohort.schema, name: 'cohort', source_jobs: [],
      acknowledge_simulation_limits: false}, source_jobs: catalogs.cohortJobs,
    limitation: 'Cohort limits'},
  '/api/v1/lens-sensor-quality-studies/template': {
    specification: jointQualityTemplate, source_jobs: catalogs.jointQualityJobs,
    limitation: 'Joint quality diagnostics only'},
  '/api/v1/lens-sensor-adaptive-demosaic/template': {
    specification: adaptiveTemplate, source_lens_sensor_jobs: catalogs.adaptiveRawJobs,
    limitation: 'Adaptive comparison only'},
  '/api/v1/lens-sensor-adaptive-demosaic/template?source_lens_sensor_job_id=lens-1': {
    specification: {...adaptiveTemplate, source_lens_sensor_job_id: 'lens-1'},
    source_lens_sensor_jobs: catalogs.adaptiveRawJobs,
    selected_source_contract: {source_binding: {source_lens_sensor_job_id: 'lens-1'},
      native_cfa_contract: 'three_channel_rggb_2x2', raw_shape: [16, 16]},
    limitation: 'Adaptive comparison only'},
  '/api/v1/lens-sensor-isp-detectors/template': {
    specification: detectorTemplate, source_lens_isp_jobs: catalogs.detectorIspJobs,
    availability: {status: 'ready_for_development_inference'},
    limitation: 'Tiny-frame detector only'},
  '/api/v1/lens-sensor-isp-detectors/template?source_lens_sensor_job_id=lens-1&source_lens_sensor_isp_job_id=isp-1': {
    specification: detectorTemplate, source_lens_isp_jobs: catalogs.detectorIspJobs,
    availability: {status: 'ready_for_development_inference'},
    image_binding: {lens_raw_job_id: 'lens-1', lens_isp_job_id: 'isp-1', color_encoding: 'srgb'},
    limitation: 'Tiny-frame detector only'},
  '/api/v1/saved-sensor-quality/quality-1': {id: 'quality-1', job_kind: INTEGRATION.quality.kind,
    status: 'succeeded', result: {summary: {schema: INTEGRATION.quality.schema, status: 'completed',
      sensor_reports: [{revision: {report_code: 'DEP-2209-801'},
        cases: [{scene: 'uniform', frame_seeds: [7]}]}]}}},
  '/api/v1/lens-sensor-isp-replays/isp-1': {id: 'isp-1', job_kind: INTEGRATION.isp.kind,
    status: 'succeeded', result: {summary: ispJobSummary},
    artifacts: [{name: 'saved-sensor-isp-replay.npz', sha256: 'a'.repeat(64),
      url: '/api/v1/artifacts/' + 'a'.repeat(64)}]},
  '/api/v1/lens-sensor-adaptive-demosaic/adaptive-1': {
    id: 'adaptive-1', job_kind: INTEGRATION.adaptive.kind, status: 'succeeded',
    result: {summary: {schema: INTEGRATION.adaptive.schema, status: 'completed',
      source_binding: {source_lens_sensor_job_id: 'lens-1'}, raw_content_sha256: 'a'.repeat(64),
      report: {method: {id: 'adaptive_laplacian_v1'}, before_after: {valid_pixels: 64,
        mean_absolute_sensor_rgb_difference: .1}}, limitation: 'Adaptive comparison only'}},
    artifacts: []},
  '/api/v1/lens-sensor-isp-detectors/detector-1': {
    id: 'detector-1', job_kind: INTEGRATION.detector.kind, status: 'succeeded',
    result: {summary: {schema: INTEGRATION.detector.schema, status: 'completed',
      specification: detector, source_identities: {lens_raw_job_id: 'lens-1', lens_isp_job_id: 'isp-1'},
      inference: {detections: []}, verification: {detector_inference: 'performed_by_offline_ultralytics_yolo11n'},
      limitation: 'Tiny-frame detector only'}}, artifacts: []},
  '/api/v1/lens-sensor-quality-studies/matrix-study-1': {
    id: 'matrix-study-1', job_kind: INTEGRATION.jointQuality.kind, status: 'succeeded',
    result: {summary: {schema: INTEGRATION.jointQuality.schema, status: 'completed',
      cases: {L1S1: {edge_mtf50: {available: false, reason: 'Too small'}}},
      comparison: {matched_horizontal_fov: true, matched_native_output_angular_grid: false,
        distortion_aware_point_center_map_by_slot: {
          L1S1: true, L1S2: true, L2S1: true, L2S2: true},
        common_550nm_point_center_roi: {nonempty: true},
        source_step_over_min_distorted_step_by_slot: {
          L1S1: 1.1, L1S2: 1.2, L2S1: 1.3, L2S2: 1.4},
        common_grid_object_angle_edge_mtf50: {
          available: true, angular_roi_area_matched: true, angular_sample_grid_matched: true,
          product_ranking_allowed: false, grid: {shape_row_col: [20, 20]},
          per_slot: {L1S1: {available: true, mtf50_cycles_per_degree_object_angle: 70}}},
        edge_mtf50_diagnostic_delta_allowed: false,
        product_ranking_allowed: false, reasons: ['FOV differs']},
      limitation: 'Model diagnostics only'}}, artifacts: []},
  '/api/v1/retained-raw-datasets/labeled-1': {id: 'labeled-1', job_kind: INTEGRATION.dataset.kind,
    status: 'succeeded', result: {summary: {schema: labeledDatasetTemplate.schema,
      status: 'completed', source_kind: 'lens_sensor_raw', labels: 'user_provided_unverified',
      raw_shape: [8, 8], raw_dtype: 'int32', raw_values_byte_equal: true,
      dataset_validation: {ok: true}}}, artifacts: []},
};
for (const contract of Object.values(INTEGRATION)) responses[`/api/v1/${contract.path}`] = [];
async function fetchMock(path, options = {}) {
  requests.push({path, options});
  let data = responses[path];
  if (path === '/api/v1/lens-sensor-isp-replays' && options.method === 'POST') {
    data = {id: 'isp-1', job_kind: INTEGRATION.isp.kind};
  }
  if (path === '/api/v1/lens-sensor-quality-studies' && options.method === 'POST') {
    data = {id: 'matrix-study-1', job_kind: INTEGRATION.jointQuality.kind};
  }
  if (path === '/api/v1/lens-sensor-pwm-hdr' && options.method === 'POST') {
    data = {id: 'pwm-hdr-1', job_kind: INTEGRATION.pwmHdr.kind};
  }
  if (path === '/api/v1/lens-sensor-adaptive-demosaic' && options.method === 'POST') {
    data = {id: 'adaptive-1', job_kind: INTEGRATION.adaptive.kind};
  }
  if (path === '/api/v1/lens-sensor-isp-detectors' && options.method === 'POST') {
    data = {id: 'detector-1', job_kind: INTEGRATION.detector.kind};
  }
  if (path === '/api/v1/retained-raw-datasets/annotations' && options.method === 'POST') {
    data = {source_job_id: 'lens-1', verification: 'user_supplied_annotation_contract_only',
      computed_sha256: 'a'.repeat(64), artifact: {sha256: 'a'.repeat(64)}};
  }
  if (path === '/api/v1/retained-raw-datasets' && options.method === 'POST') {
    data = {id: 'labeled-1', job_kind: INTEGRATION.dataset.kind};
  }
  if (data === undefined) throw new Error('Unexpected browser request ' + path);
  return {ok: true, status: 200, text: async () => JSON.stringify(data)};
}

async function browserContract() {
  const source = fs.readFileSync(require.resolve('../../../src/camera_platform/static/integration-workbench.js'), 'utf8');
  vm.runInNewContext(source, {document: browserDocument, fetch: fetchMock,
    history: {replaceState() {}}, location: {search: ''},
    setTimeout, clearTimeout, URLSearchParams, console}, {filename: 'integration-workbench.js'});
  for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));
  assert.ok(requests.some(item => item.path === '/api/v1/lens-sensor-isp-replays/template'));
  assert.ok(requests.some(item => item.path === '/api/v1/lens-sensor-raw/template?scope=local_field_psf'));
  assert.ok(requests.some(item => item.path === '/api/v1/lens-sensor-raw/template?scope=central_bounded'));
  assert.ok(requests.some(item => item.path === '/api/v1/lens-sensor-raw/template?scope=central_bounded_common_scene'));
  assert.ok(requests.some(item => item.path === '/api/v1/lens-sensor-raw/template?scope=central_bounded_dense_common_scene'));
  assert.ok(requests.some(item => item.path === '/api/v1/lens-sensor-hdr/template?scope=central_bounded'));
  assert.ok(requests.some(item => item.path === '/api/v1/lens-sensor-lfm/template?scope=central_bounded'));
  assert.ok(requests.some(item => item.path === '/api/v1/lens-sensor-lfm/template?scope=central_bounded_timed'));
  assert.ok(requests.some(item => item.path === '/api/v1/lens-sensor-quality-studies/template'));
  assert.ok(requests.some(item => item.path === '/api/v1/lens-sensor-pwm-hdr/template'));
  assert.ok(requests.some(item => item.path === '/api/v1/lens-sensor-adaptive-demosaic/template'));
  assert.ok(requests.some(item => item.path === '/api/v1/lens-sensor-isp-detectors/template'));
  assert.ok(requests.some(item => item.path === '/api/v1/retained-raw-datasets/template?source_kind=lens_sensor_raw'));
  assert.ok(requests.some(item => item.path === '/api/v1/retained-raw-datasets/template?source_kind=lens_sensor_raw_labeled'));
  assert.equal(element('lens-revision').children.length, 2, 'Only executable Lens revisions are shown initially');
  assert.match(element('lens-revision-count').textContent, /실행 가능 1건 · 전체 2건/);
  element('lens-revision-all').checked = true;
  await element('lens-revision-all').dispatch('change');
  assert.equal(element('lens-revision').children.length, 3);
  element('lens-revision-query').value = 'blocked-rev';
  await element('lens-revision-query').dispatch('input');
  assert.equal(element('lens-revision').children.length, 2);
  assert.equal(element('lens-revision').children[1].disabled, true);
  assert.equal(element('isp-name').value, 'RAW ISP');
  assert.equal(element('isp-ccm').value, '1, 0, 0, 0, 1, 0, 0, 0, 1');
  assert.ok((element('isp-form').listeners.submit || []).length, 'ISP submit handler required');
  element('isp-source').value = 'lens-1';
  await element('isp-source').dispatch('change');
  element('isp-ack').checked = true;
  await element('isp-form').dispatch('submit');
  const posted = requests.find(item => item.path === '/api/v1/lens-sensor-isp-replays' && item.options.method === 'POST');
  assert.ok(posted, 'ISP POST required');
  const body = JSON.parse(posted.options.body);
  assert.equal(body.source_lens_sensor_job_id, 'lens-1');
  assert.equal(body.profile.spatial_lsc.mode, 'bypass');
  assert.equal(element('status').textContent.includes('succeeded'), true);
  const rendered = JSON.stringify(element('result-summary'));
  assert.match(rendered, /ISP 차이/);
  assert.match(rendered, /유효 픽셀 비율/);
  assert.match(rendered, /화질 개선·인지 점수/);
  assert.equal(element('artifacts').children.length, 1);
  assert.equal(element('lens-revision-count').textContent.includes('전체 2건'), true);
  element('lens-scope').value = 'local_field_psf';
  await element('lens-scope').dispatch('change');
  assert.equal(element('lens-local-fields').hidden, false);
  assert.equal(element('lens-limits').textContent, 'Local PSF only');
  element('lens-scope').value = 'central_bounded';
  await element('lens-scope').dispatch('change');
  assert.equal(element('lens-local-fields').hidden, true);
  assert.equal(element('lens-limits').textContent, 'Bounded strict PSF');
  element('lens-scope').value = 'central_bounded_common_scene';
  await element('lens-scope').dispatch('change');
  assert.equal(element('lens-common-scene-fields').hidden, false);
  assert.equal(element('lens-common-fov').value, .2);
  assert.equal(element('lens-limits').textContent, 'Common input scene only');
  element('lens-scope').value = 'central_bounded_dense_common_scene';
  await element('lens-scope').dispatch('change');
  assert.equal(element('lens-dense-scene-fields').hidden, false);
  assert.equal(element('lens-scene-samples-rows').value, 128);
  assert.equal(element('lens-limits').textContent, 'Dense common input scene only');
  element('dataset-source-kind').value = 'lens_sensor_raw';
  await element('dataset-source-kind').dispatch('change');
  assert.equal(element('dataset-case-fields').hidden, true);
  assert.ok(element('dataset-source').children.some(item => item.value === 'lens-1'));
  element('dataset-source-kind').value = 'lens_sensor_raw_labeled';
  await element('dataset-source-kind').dispatch('change');
  element('dataset-source').value = 'lens-1';
  await element('dataset-source').dispatch('change');
  assert.equal(element('dataset-labeled-fields').hidden, false);
  assert.match(element('dataset-labeled-shape').textContent, /8×8/);
  element('dataset-label-provider').value = 'fixture-author';
  element('dataset-label-set').value = 'fixture-v1';
  element('dataset-label-method').value = 'human_review';
  element('dataset-label-coverage').value = 'partial';
  element('dataset-label-objects').value = '[{"label":"object","bbox_xyxy":[1,1,5,5]}]';
  element('dataset-ack').checked = true;
  await element('dataset-form').dispatch('submit');
  const annotationPost = requests.find(item => item.path === '/api/v1/retained-raw-datasets/annotations' &&
    item.options.method === 'POST');
  assert.ok(annotationPost, 'Server-side annotation import required');
  assert.deepEqual(JSON.parse(annotationPost.options.body).document.objects, annotation.objects);
  const labeledPost = requests.find(item => item.path === '/api/v1/retained-raw-datasets' &&
    item.options.method === 'POST');
  assert.equal(JSON.parse(labeledPost.options.body).annotation_artifact_sha256, 'a'.repeat(64));
  assert.equal(element('hdr-scope').value, 'central_bounded');
  assert.equal(element('lfm-scope').value, 'central_bounded');
  element('lfm-scope').value = 'central_bounded_timed';
  await element('lfm-scope').dispatch('change');
  assert.equal(element('lfm-timing-fields').hidden, false);
  assert.equal(element('lfm-timing-sha').required, true);
  assert.ok(element('pwmHdr-source').children.some(item => item.value === 'lfm-1'));
  element('pwmHdr-source').value = 'lfm-1';
  element('pwmHdr-ack').checked = true;
  await element('pwmHdr-form').dispatch('submit');
  const pwmHdrPost = requests.find(item => item.path === '/api/v1/lens-sensor-pwm-hdr' &&
    item.options.method === 'POST');
  assert.ok(pwmHdrPost, 'Saved PWM HDR POST required');
  assert.equal(JSON.parse(pwmHdrPost.options.body).source_lfm_job_id, 'lfm-1');
  assert.match(JSON.stringify(element('result-summary')), /두 노출 융합/);
  assert.match(JSON.stringify(element('result-summary')), /무효 픽셀/);
  assert.equal(element('jointQuality-L1S1').children.length, 5, element('error').textContent);
  assert.ok((element('jointQuality-form').listeners.submit || []).length);
  for (const slot of ['L1S1', 'L1S2', 'L2S1', 'L2S2']) {
    element(`jointQuality-${slot}`).value = `matrix-${slot}`;
  }
  element('jointQuality-ack').checked = true;
  await element('jointQuality-form').dispatch('submit');
  const qualityPost = requests.find(item => item.path === '/api/v1/lens-sensor-quality-studies' &&
    item.options.method === 'POST');
  assert.ok(qualityPost, 'Lens × Sensor quality POST required');
  assert.deepEqual(JSON.parse(qualityPost.options.body).source_jobs, matrixInput.sourceJobs);
  assert.match(JSON.stringify(element('result-summary')), /제품 순위/);
  assert.match(JSON.stringify(element('result-summary')), /왜곡 포함 픽셀 중심 좌표/);
  assert.match(JSON.stringify(element('result-summary')), /모델 좌표 겹침/);
  assert.match(JSON.stringify(element('result-summary')), /기하 좌표 면적\/격자 일치/);
  assert.match(JSON.stringify(element('result-summary')), /픽셀 적분 미검증/);
  element('adaptive-source').value = 'lens-1';
  element('adaptive-ack').checked = true;
  await element('adaptive-form').dispatch('submit');
  const adaptivePost = requests.find(item => item.path === '/api/v1/lens-sensor-adaptive-demosaic' &&
    item.options.method === 'POST');
  assert.ok(adaptivePost, element('error').textContent);
  assert.equal(JSON.parse(adaptivePost.options.body).source_lens_sensor_job_id, 'lens-1');
  assert.match(JSON.stringify(element('result-summary')), /Bilinear 대비 차이/);
  element('detector-raw').value = 'lens-1';
  element('detector-isp').value = 'isp-1';
  element('detector-ack').checked = true;
  await element('detector-form').dispatch('submit');
  const detectorPost = requests.find(item => item.path === '/api/v1/lens-sensor-isp-detectors' &&
    item.options.method === 'POST');
  assert.ok(detectorPost, element('error').textContent);
  assert.equal(JSON.parse(detectorPost.options.body).source_lens_sensor_isp_job_id, 'isp-1');
  assert.match(JSON.stringify(element('result-summary')), /제품 인지 성능/);
  const selectedDetectorTemplate = '/api/v1/lens-sensor-isp-detectors/template?' +
    'source_lens_sensor_job_id=lens-1&source_lens_sensor_isp_job_id=isp-1';
  responses[selectedDetectorTemplate] = {...responses[selectedDetectorTemplate],
    availability: {status: 'operator_trust_required'},
    specification: {...detectorTemplate, model_weights_sha256: ''}};
  const detectorPostCount = requests.filter(item => item.path === '/api/v1/lens-sensor-isp-detectors' &&
    item.options.method === 'POST').length;
  await element('detector-form').dispatch('submit');
  assert.equal(requests.filter(item => item.path === '/api/v1/lens-sensor-isp-detectors' &&
    item.options.method === 'POST').length, detectorPostCount);
  assert.match(element('error').textContent, /신뢰|가중치/);
  responses['/api/v1/lens-sensor-isp-detectors/template'] = {
    ...responses['/api/v1/lens-sensor-isp-detectors/template'],
    availability: {status: 'operator_trust_required'},
    specification: {...detectorTemplate, model_weights_sha256: ''},
  };
  await element('cohort-refresh').dispatch('click');
  assert.equal(element('detector-submit').disabled, true);
  assert.equal(element('detector-weights').value, '');
  assert.match(element('detector-limits').textContent, /잠김/);
  responses['/api/v1/lens-sensor-lfm/lfm-new'] = {
    id: 'lfm-new', job_kind: INTEGRATION.lfm.kind, status: 'succeeded',
    result: {summary: {schema: boundedLfmTemplate.schema, status: 'completed',
      report: {cases: [], verification: {physical_validation: 'not_established'}}}},
    artifacts: [],
  };
  responses['/api/v1/lens-sensor-pwm-hdr/template'] = {
    ...responses['/api/v1/lens-sensor-pwm-hdr/template'],
    source_lfm_jobs: [...catalogs.pwmHdrLfmJobs,
      {id: 'lfm-new', name: 'New saved LFM', status: 'succeeded'}],
  };
  element('open-kind').value = 'lfm';
  element('open-id').value = 'lfm-new';
  await element('open-form').dispatch('submit');
  for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve));
  assert.ok(element('pwmHdr-source').children.some(item => item.value === 'lfm-new'),
    'Completed LFM must become selectable for saved PWM HDR without manual refresh');
}

browserContract().then(() => console.log('integration workbench contracts passed')).catch(error => {
  console.error(error); process.exitCode = 1;
});

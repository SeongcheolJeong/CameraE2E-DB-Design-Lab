'use strict';

const INTEGRATION = Object.freeze({
  lens: {path: 'lens-sensor-raw', kind: 'lens_sensor_raw', schema: 'camerae2e_lens_sensor_raw_preview_study_v1', label: '렌즈×센서'},
  hdr: {path: 'lens-sensor-hdr', kind: 'lens_sensor_hdr', schema: 'camerae2e_lens_sensor_static_hdr_study_v1', label: '결합 HDR'},
  lfm: {path: 'lens-sensor-lfm', kind: 'lens_sensor_lfm', schema: 'camerae2e_lens_sensor_pwm_lfm_study_v1', label: '결합 LFM'},
  pwmHdr: {path: 'lens-sensor-pwm-hdr', kind: 'lens_sensor_pwm_hdr', schema: 'camerae2e_lens_sensor_pwm_hdr_study_v1', label: '저장 PWM HDR'},
  isp: {path: 'lens-sensor-isp-replays', kind: 'lens_sensor_isp_replay', schema: 'camerae2e_lens_sensor_isp_replay_study_v1', label: '결합 RAW ISP'},
  dataset: {path: 'retained-raw-datasets', kind: 'retained_raw_dataset', schema: 'camerae2e_retained_raw_dataset_study_v1', label: 'RAW 데이터셋'},
  cohort: {path: 'lens-sensor-raw-cohorts', kind: 'lens_sensor_raw_cohort', schema: 'camerae2e_lens_sensor_raw_cohort_study_v1', label: 'RAW cohort'},
  jointQuality: {path: 'lens-sensor-quality-studies', kind: 'lens_sensor_quality_study', schema: 'camerae2e_lens_sensor_quality_study_v2', label: '결합 2×2 비교'},
  adaptive: {path: 'lens-sensor-adaptive-demosaic', kind: 'lens_sensor_adaptive_demosaic', schema: 'camerae2e_lens_sensor_adaptive_demosaic_study_v1', label: 'Adaptive demosaic'},
  detector: {path: 'lens-sensor-isp-detectors', kind: 'lens_sensor_isp_detector', schema: 'camerae2e_lens_sensor_isp_detector_study_v1', label: '로컬 검출기'},
  quality: {path: 'saved-sensor-quality', kind: 'saved_sensor_quality', schema: 'camerae2e_saved_sensor_quality_study_v1', label: '저장 화질'},
});
const DATASET_LENS_SCHEMA = 'camerae2e_retained_raw_dataset_study_v2';
const DATASET_LABELED_SCHEMA = 'camerae2e_retained_raw_dataset_study_v3';
const LOCAL_LENS_SCHEMA = 'camerae2e_lens_sensor_raw_preview_study_v2';
const BOUNDED_LENS_SCHEMA = 'camerae2e_lens_sensor_raw_preview_study_v3';
const COMMON_SCENE_LENS_SCHEMA = 'camerae2e_lens_sensor_raw_preview_study_v4';
const DENSE_COMMON_SCENE_LENS_SCHEMA = 'camerae2e_lens_sensor_raw_preview_study_v5';
const BOUNDED_HDR_SCHEMA = 'camerae2e_lens_sensor_static_hdr_study_v2';
const BOUNDED_LFM_SCHEMA = 'camerae2e_lens_sensor_pwm_lfm_study_v2';
const TIMED_LFM_SCHEMA = 'camerae2e_lens_sensor_pwm_lfm_study_v3';
const EDITABLE_KINDS = Object.freeze(['lens', 'hdr', 'lfm', 'pwmHdr', 'isp', 'dataset', 'cohort', 'jointQuality', 'adaptive', 'detector']);

function integrationNumber(value, name, low, high, integer = false) {
  if (value === undefined || value === null || String(value).trim() === '') {
    throw new Error(`${name} 값을 입력하세요.`);
  }
  const number = Number(value);
  if (!Number.isFinite(number) || number < low || number > high || (integer && !Number.isInteger(number))) {
    throw new Error(`${name} 값은 ${low}–${high}${integer ? ' 정수' : ''}여야 합니다.`);
  }
  return number;
}

function integrationAxisMs(value, name, limit = 4) {
  const pieces = String(value ?? '').split(',').map(part => part.trim());
  if (!pieces.length || pieces.length > limit || pieces.some(part => !part)) {
    throw new Error(`${name} 값을 쉼표로 구분해 1–${limit}개 입력하세요.`);
  }
  const seconds = pieces.map(part => integrationNumber(part, name, .001, 1000) / 1000);
  if (new Set(seconds).size !== seconds.length) throw new Error(`${name} 값은 서로 달라야 합니다.`);
  return seconds;
}

function integrationSource(catalog, runId, allowedModels = null) {
  const row = catalog.find(item => item.run_id === runId);
  if (!row || row.status !== 'simulated' || !row.run_id || !(row.report_code || row.sensor_id) ||
      (allowedModels && !allowedModels.includes(row.model))) {
    throw new Error('지원되는 저장 센서 run을 선택하세요. 최종 계약은 제출 전에 다시 검사합니다.');
  }
  return {identifier: row.report_code || row.sensor_id, run_id: row.run_id};
}

function integrationSelectableLenses(rows, query = '', includeUnsupported = false) {
  if (!Array.isArray(rows)) throw new Error('렌즈 catalog 목록 계약이 잘못됐습니다.');
  const term = String(query).trim().toLocaleLowerCase();
  return rows.filter(row => {
    if (row?.preview_status !== 'ready' && !includeUnsupported) return false;
    if (!term) return true;
    return [row?.manufacturer, row?.name, row?.simulation_id, row?.revision_id,
      row?.configuration].some(value => String(value || '').toLocaleLowerCase().includes(term));
  });
}

function integrationCcm(value) {
  const parts = String(value ?? '').split(',').map(item => item.trim());
  if (parts.length !== 9 || parts.some(item => !item)) {
    throw new Error('CCM 계수를 출력 R/G/B 행 순서로 9개 입력하세요.');
  }
  const numbers = parts.map(item => integrationNumber(item, 'CCM 계수', -8, 8));
  const rows = [numbers.slice(0, 3), numbers.slice(3, 6), numbers.slice(6, 9)];
  if (rows.some(row => row.reduce((total, item) => total + Math.abs(item), 0) > 16)) {
    throw new Error('CCM 각 행의 절댓값 합은 16 이하여야 합니다.');
  }
  return rows;
}

function integrationExactFields(template, fields, kind) {
  if (Object.keys(template).sort().join('|') !== [...fields].sort().join('|')) {
    throw new Error(`${kind} 서버 template가 이 화면의 입력 계약과 다릅니다.`);
  }
}

function integrationAnnotationDocument(template, input) {
  if (template?.schema !== 'camerae2e_retained_raw_annotations_v1' ||
      template?.coordinate_frame !== 'raw_mosaic_pixel_xyxy_half_open' ||
      template?.source_binding?.source_lens_sensor_job_id !== input.source ||
      !Array.isArray(template.raw_shape) || template.raw_shape.length !== 2 ||
      template.raw_shape.some(value => !Number.isInteger(value) || value <= 0)) {
    throw new Error('선택한 RAW의 서버 annotation template을 먼저 불러오세요.');
  }
  const provider = String(input.labelProvider || '').trim();
  const annotationSet = String(input.labelSet || '').trim();
  if (!provider || provider.length > 160 || !annotationSet || annotationSet.length > 160) {
    throw new Error('라벨 제공자와 annotation set ID를 1–160자로 입력하세요.');
  }
  if (!['human_review', 'external_reference', 'synthetic_author'].includes(input.labelMethod) ||
      !['full_frame_reviewed', 'partial'].includes(input.labelCoverage)) {
    throw new Error('라벨 작성 방법과 검토 범위를 선택하세요.');
  }
  let objects;
  try { objects = JSON.parse(String(input.labelObjects || '')); }
  catch { throw new Error('객체 목록은 JSON 배열이어야 합니다.'); }
  if (!Array.isArray(objects) || objects.length > 2048) {
    throw new Error('객체 목록은 최대 2048개의 JSON 배열이어야 합니다.');
  }
  const [height, width] = template.raw_shape;
  for (const [index, item] of objects.entries()) {
    const label = item?.label, box = item?.bbox_xyxy;
    if (Object.keys(item || {}).sort().join('|') !== 'bbox_xyxy|label' ||
        typeof label !== 'string' || !label.trim() || label.length > 128 ||
        /[\u0000-\u001f]/.test(label) || !Array.isArray(box) || box.length !== 4 ||
        box.some(value => typeof value !== 'number' || !Number.isFinite(value)) ||
        !(0 <= box[0] && box[0] < box[2] && box[2] <= width &&
          0 <= box[1] && box[1] < box[3] && box[3] <= height)) {
      throw new Error(`객체 ${index + 1}의 라벨 또는 RAW 반개구간 박스가 잘못됐습니다.`);
    }
  }
  if (!objects.length && input.labelCoverage !== 'full_frame_reviewed') {
    throw new Error('빈 라벨은 전체 프레임 검토를 선언해야 합니다.');
  }
  const document = JSON.parse(JSON.stringify(template));
  document.provenance = {provider_id: provider, annotation_set_id: annotationSet,
    method: input.labelMethod, coverage: input.labelCoverage};
  document.objects = objects;
  return document;
}

function integrationSpec(kind, template, catalogs, input) {
  if (!INTEGRATION[kind] || (template?.schema !== INTEGRATION[kind].schema &&
      !(kind === 'dataset' && [DATASET_LENS_SCHEMA, DATASET_LABELED_SCHEMA].includes(template?.schema)) &&
      !(kind === 'lens' && [LOCAL_LENS_SCHEMA, BOUNDED_LENS_SCHEMA,
        COMMON_SCENE_LENS_SCHEMA, DENSE_COMMON_SCENE_LENS_SCHEMA].includes(template?.schema)) &&
      !(kind === 'hdr' && template?.schema === BOUNDED_HDR_SCHEMA) &&
      !(kind === 'lfm' && [BOUNDED_LFM_SCHEMA, TIMED_LFM_SCHEMA].includes(template?.schema)))) {
    throw new Error('서버와 화면의 타입별 입력 계약이 다릅니다.');
  }
  const name = String(input.name || '').trim();
  if (!name || name.length > 160) throw new Error('작업 이름은 1–160자로 입력하세요.');
  if (input.ack !== true) throw new Error('모델과 결과의 적용 범위를 확인하세요.');
  const result = {...template, name};
  if (kind === 'lens') {
    const lens = catalogs.lenses.find(item => item.revision_id === input.lens);
    if (!lens || lens.preview_status !== 'ready') throw new Error('저장 PSF 자산 검사를 통과한 렌즈 revision을 선택하세요.');
    const local = template.schema === LOCAL_LENS_SCHEMA;
    const denseScene = template.schema === DENSE_COMMON_SCENE_LENS_SCHEMA;
    const commonScene = template.schema === COMMON_SCENE_LENS_SCHEMA || denseScene;
    const expectedScope = local ? 'local_field_psf' :
      denseScene ? 'central_bounded_dense_common_scene' :
      commonScene ? 'central_bounded_common_scene' :
      template.schema === BOUNDED_LENS_SCHEMA ? 'central_bounded' : 'central';
    if ((input.scope || 'central') !== expectedScope) {
      throw new Error('렌즈 시야 범위와 template 계약이 다릅니다.');
    }
    return {
      ...result,
      lens_revision_id: lens.revision_id,
      sensor_revision: integrationSource(catalogs.sensors, input.sensor, ['db_estimate_v1']),
      scene_kind: input.scene,
      sensor_size: [integrationNumber(input.rows, 'RAW 행', 8, 32, true), integrationNumber(input.cols, 'RAW 열', 8, 32, true)],
      luminance_cd_m2: integrationNumber(input.luminance, '휘도', .01, 10000),
      exposure_s: integrationNumber(input.exposureMs, '노출', .001, 1000) / 1000,
      noise_flag: integrationNumber(input.noise, '잡음', 0, 2, true),
      seed: integrationNumber(input.seed, 'seed', 0, 2 ** 32 - 1, true),
      accept_approximate_lens_radiometry: true,
      ...(commonScene ? {scene_horizontal_fov_deg:
        integrationNumber(input.sceneHorizontalFovDeg, '공통 입력 장면 화각', .001, 30)} : {}),
      ...(denseScene ? {scene_samples_row_col: [
        integrationNumber(input.sceneSamplesRows, '입력 장면 표본 행', 32, 256, true),
        integrationNumber(input.sceneSamplesCols, '입력 장면 표본 열', 32, 256, true),
      ]} : {}),
      ...(local ? {field_psf_index: integrationNumber(input.fieldIndex, 'PSF 시야 index', 0, 64, true),
        field_azimuth_deg: integrationNumber(input.fieldAzimuth, '방위각', 0, 359.999)} : {}),
    };
  }
  if (kind === 'hdr') {
    integrationExactFields(template, ['schema', 'name', 'lens_revision_id', 'sensor_revision', 'exposure_s',
      'scene_kind', 'sensor_size', 'luminance_cd_m2', 'seed', 'accept_approximate_lens_radiometry'], 'HDR');
    const lens = catalogs.hdrLenses.find(item => item.revision_id === input.lens);
    if (!lens || lens.preview_status !== 'ready') throw new Error('HDR에 사용할 저장 PSF 자산 검사 통과 렌즈를 선택하세요.');
    if ((input.scope || 'central') !== (template.schema === BOUNDED_HDR_SCHEMA ? 'central_bounded' : 'central')) {
      throw new Error('HDR PSF 실행 범위와 template 계약이 다릅니다.');
    }
    const exposures = integrationAxisMs(input.exposuresMs, '노출', 2);
    if (exposures.length !== 2 || exposures[0] >= exposures[1]) {
      throw new Error('결합 HDR에는 오름차순 노출 두 개가 필요합니다.');
    }
    return {
      ...result,
      lens_revision_id: lens.revision_id,
      sensor_revision: integrationSource(catalogs.hdrSensors, input.sensor, ['db_estimate_v1']),
      exposure_s: exposures,
      scene_kind: input.scene,
      sensor_size: [integrationEvenSize(input.rows, 'HDR RAW 행'), integrationEvenSize(input.cols, 'HDR RAW 열')],
      luminance_cd_m2: integrationNumber(input.luminance, '휘도', .01, 10000),
      seed: integrationNumber(input.seed, 'seed', 0, 2 ** 32 - 1, true),
      accept_approximate_lens_radiometry: true,
    };
  }
  if (kind === 'lfm') {
    const expected = ['schema', 'name', 'lens_revision_id', 'sensor_revision', 'sensor_size', 'exposure_s',
      'pwm_period_s', 'duty_cycle', 'off_to_on_ratio', 'on_luminance_cd_m2', 'seed',
      'accept_approximate_lens_radiometry', 'acknowledge_simulation_limits',
      ...(template.schema === TIMED_LFM_SCHEMA ? ['timing_mode_sha256'] : [])];
    integrationExactFields(template, expected, 'LFM');
    const lens = catalogs.lfmLenses.find(item => item.revision_id === input.lens);
    if (!lens || lens.preview_status !== 'ready') throw new Error('LFM에 사용할 저장 PSF 자산 검사 통과 렌즈를 선택하세요.');
    const expectedScope = template.schema === TIMED_LFM_SCHEMA ? 'central_bounded_timed' :
      template.schema === BOUNDED_LFM_SCHEMA ? 'central_bounded' : 'central';
    if ((input.scope || 'central') !== expectedScope) {
      throw new Error('LFM PSF 실행 범위와 template 계약이 다릅니다.');
    }
    if (template.schema === TIMED_LFM_SCHEMA && !/^[0-9a-f]{64}$/.test(input.timingModeSha256 || '')) {
      throw new Error('센서·노출쌍에 결속해 등록한 시간 모드 SHA-256을 입력하세요.');
    }
    const exposures = integrationAxisMs(input.exposuresMs, '노출', 2);
    if (exposures.length !== 2 || exposures.some(item => item < .0005 || item > .02) || exposures[0] >= exposures[1]) {
      throw new Error('LFM 노출은 오름차순 0.5–20 ms 두 개여야 합니다.');
    }
    const period = integrationNumber(input.periodMs, 'PWM 주기', .5, 20) / 1000;
    if (exposures[1] / period > 32) throw new Error('최대 노출은 PWM 주기 32회 이내여야 합니다.');
    return {
      ...result,
      lens_revision_id: lens.revision_id,
      sensor_revision: integrationSource(catalogs.lfmSensors, input.sensor, ['db_estimate_v1']),
      sensor_size: [integrationEvenSize(input.rows, 'LFM RAW 행'), integrationEvenSize(input.cols, 'LFM RAW 열')],
      exposure_s: exposures,
      pwm_period_s: period,
      duty_cycle: integrationNumber(input.duty, 'Duty cycle', .05, .95),
      off_to_on_ratio: integrationNumber(input.offRatio, 'Off/On 비', 0, .5),
      on_luminance_cd_m2: integrationNumber(input.onLuminance, 'On 휘도', .01, 10000),
      seed: integrationNumber(input.seed, 'seed', 0, 2 ** 32 - 1, true),
      accept_approximate_lens_radiometry: true,
      acknowledge_simulation_limits: true,
      ...(template.schema === TIMED_LFM_SCHEMA ? {timing_mode_sha256: input.timingModeSha256} : {}),
    };
  }
  if (kind === 'pwmHdr') {
    integrationExactFields(template, ['schema', 'name', 'source_lfm_job_id',
      'acknowledge_simulation_limits'], '저장 PWM HDR');
    const source = catalogs.pwmHdrLfmJobs.find(item =>
      item.id === input.source && item.status === 'succeeded');
    if (!source) throw new Error('완료된 저장 Lens × Sensor PWM/LFM 작업을 선택하세요.');
    return {...result, source_lfm_job_id: source.id, acknowledge_simulation_limits: true};
  }
  if (kind === 'isp') {
    integrationExactFields(template, ['schema', 'name', 'source_lens_sensor_job_id', 'profile',
      'acknowledge_simulation_limits'], 'ISP');
    const source = catalogs.ispLensJobs.find(item => item.id === input.source && item.status === 'succeeded');
    if (!source) throw new Error('ISP template에 포함된 완료 Lens × Sensor RAW 작업을 선택하세요.');
    const profile = JSON.parse(JSON.stringify(template.profile));
    if (profile?.schema !== 'camera_isp_profile_v2' || profile.calibration_id !== null ||
        profile.spatial_lsc?.mode !== 'bypass' || profile.raw_denoise?.mode !== 'bypass') {
      throw new Error('저장 RAW ISP profile은 무보정 LSC·denoise bypass여야 합니다.');
    }
    profile.white_balance.gains_rgb = [
      integrationNumber(input.wbR, 'WB R gain', 1 / 16, 16),
      integrationNumber(input.wbG, 'WB G gain', 1 / 16, 16),
      integrationNumber(input.wbB, 'WB B gain', 1 / 16, 16),
    ];
    profile.ccm.matrix_rgb = integrationCcm(input.ccm);
    if (!['srgb', 'linear'].includes(input.tone)) throw new Error('Tone은 sRGB 또는 linear여야 합니다.');
    profile.tone.curve = input.tone;
    profile.linear_sharpen.mode = input.sharpen === true ? 'bounded_detail_v1' : 'bypass';
    profile.linear_sharpen.amount = integrationNumber(input.sharpenAmount, 'Sharpen amount', 0, 1);
    return {
      ...result,
      source_lens_sensor_job_id: source.id,
      profile,
      acknowledge_simulation_limits: true,
    };
  }
  if (kind === 'dataset') {
    if (template.schema === DATASET_LABELED_SCHEMA) {
      const source = (catalogs.datasetLensJobs || []).find(item => item.id === input.source && item.status === 'succeeded');
      if (!source || input.sourceKind !== 'lens_sensor_raw_labeled') {
        throw new Error('라벨용 완료 Lens × Sensor RAW 작업을 선택하세요.');
      }
      if (!/^[0-9a-f]{64}$/.test(input.annotationSha256 || '')) {
        throw new Error('서버에 등록한 원본 RAW 라벨 artifact 해시가 필요합니다.');
      }
      return {...result, source_kind: 'lens_sensor_raw', source_job_id: source.id,
        split: input.split, labels: 'user_provided_unverified',
        annotation_artifact_sha256: input.annotationSha256,
        acknowledge_simulation_limits: true};
    }
    if (template.schema === DATASET_LENS_SCHEMA) {
      const source = (catalogs.datasetLensJobs || []).find(item => item.id === input.source && item.status === 'succeeded');
      if (!source || input.sourceKind !== 'lens_sensor_raw') throw new Error('완료된 렌즈 × 센서 RAW 작업을 선택하세요.');
      return {...result, source_kind: 'lens_sensor_raw', source_job_id: source.id,
        split: input.split, labels: 'unlabeled', acknowledge_simulation_limits: true};
    }
    const source = catalogs.qualityJobs.find(item => item.id === input.source && item.status === 'succeeded');
    if (!source || (input.sourceKind && input.sourceKind !== 'saved_sensor_quality')) {
      throw new Error('완료된 저장 화질 작업을 선택하세요.');
    }
    return {
      ...result,
      source_quality_job_id: source.id,
      sensor_report_index: integrationNumber(input.report, '센서 보고서', 0, 2, true),
      case_index: integrationNumber(input.case, '장면', 0, 2, true),
      split: input.split,
      labels: 'unlabeled',
      acknowledge_simulation_limits: true,
    };
  }
  if (kind === 'cohort') {
    integrationExactFields(template, ['schema', 'name', 'source_jobs', 'labels',
      'acknowledge_simulation_limits'], 'cohort');
    if (!Array.isArray(input.sources) || input.sources.length < 2 || input.sources.length > 64) {
      throw new Error('완료된 Lens × Sensor RAW 작업 2–64개를 선택하세요.');
    }
    const seen = new Set();
    const sources = input.sources.map(item => {
      if (seen.has(item.id) || !catalogs.cohortJobs.some(row => row.id === item.id && row.status === 'succeeded') ||
          !['train', 'validation', 'test'].includes(item.split)) throw new Error('cohort 출처 또는 split이 올바르지 않습니다.');
      seen.add(item.id);
      return {source_job_id: item.id, split: item.split};
    });
    return {...result, source_jobs: sources, labels: 'unlabeled', acknowledge_simulation_limits: true};
  }
  if (kind === 'jointQuality') {
    integrationExactFields(template, ['schema', 'name', 'source_jobs', 'profile',
      'acknowledge_simulation_limits'], 'Lens × Sensor 2×2 비교');
    if (template.profile?.schema !== 'camera_isp_profile_v2') {
      throw new Error('고정 비교 ISP profile 계약이 다릅니다.');
    }
    const jobs = {};
    for (const slot of ['L1S1', 'L1S2', 'L2S1', 'L2S2']) {
      const id = input.sourceJobs?.[slot];
      if (!catalogs.jointQualityJobs.some(item => item.id === id && item.status === 'succeeded')) {
        throw new Error(`${slot}에 완료된 결합 RAW를 선택하세요.`);
      }
      jobs[slot] = id;
    }
    if (new Set(Object.values(jobs)).size !== 4) {
      throw new Error('서로 다른 결합 RAW 작업 4개를 선택하세요.');
    }
    return {...result, source_jobs: jobs, profile: JSON.parse(JSON.stringify(template.profile)),
      acknowledge_simulation_limits: true};
  }
  if (kind === 'adaptive') {
    integrationExactFields(template, ['schema', 'name', 'source_lens_sensor_job_id', 'method',
      'acknowledge_simulation_limits'], 'Adaptive demosaic');
    const source = (catalogs.adaptiveRawJobs || []).find(item => item.id === input.source && item.status === 'succeeded');
    if (!source || template.method !== 'adaptive_laplacian_v1') {
      throw new Error('Adaptive demosaic에 사용할 완료 RAW와 방법 계약을 확인하세요.');
    }
    return {...result, source_lens_sensor_job_id: source.id, acknowledge_simulation_limits: true};
  }
  if (kind === 'detector') {
    integrationExactFields(template, ['schema', 'name', 'source_lens_sensor_job_id',
      'source_lens_sensor_isp_job_id', 'model_profile', 'model_weights_sha256',
      'target_labels', 'score_threshold', 'annotation_artifact_sha256', 'iou_threshold',
      'acknowledge_simulation_limits'], '로컬 검출기');
    const raw = (catalogs.adaptiveRawJobs || []).find(item => item.id === input.raw && item.status === 'succeeded');
    const isp = (catalogs.detectorIspJobs || []).find(item => item.id === input.isp && item.status === 'succeeded');
    const labels = String(input.labels || '').split(',').map(value => value.trim());
    if (!raw || !isp || template.model_profile !== 'ultralytics_yolo11n_detection' ||
        !/^[0-9a-f]{64}$/.test(template.model_weights_sha256 || '') ||
        !labels.length || labels.length > 64 || labels.some(value => !value || value.length > 128) ||
        new Set(labels).size !== labels.length) {
      throw new Error('로컬 가중치와 완료 RAW·ISP·검출 class 계약을 확인하세요.');
    }
    return {...result, source_lens_sensor_job_id: raw.id,
      source_lens_sensor_isp_job_id: isp.id, target_labels: labels,
      score_threshold: integrationNumber(input.score, '검출 score 하한', .01, .95),
      annotation_artifact_sha256: '', acknowledge_simulation_limits: true};
  }
  throw new Error('이 화면에서 지원하지 않는 작업 종류입니다.');
}

function integrationSummary(kind, job) {
  const contract = INTEGRATION[kind];
  if (!contract || job?.job_kind !== contract.kind) throw new Error('선택한 작업의 종류가 다릅니다.');
  if (job.status !== 'succeeded') return null;
  const summary = job.result?.summary;
  if ((summary?.schema !== contract.schema &&
      !(kind === 'dataset' && [DATASET_LENS_SCHEMA, DATASET_LABELED_SCHEMA].includes(summary?.schema)) &&
      !(kind === 'lens' && [LOCAL_LENS_SCHEMA, BOUNDED_LENS_SCHEMA,
        COMMON_SCENE_LENS_SCHEMA].includes(summary?.schema)) &&
      !(kind === 'hdr' && summary?.schema === BOUNDED_HDR_SCHEMA) &&
      !(kind === 'lfm' && summary?.schema === BOUNDED_LFM_SCHEMA)) ||
      !((['hdr', 'pwmHdr'].includes(kind) && summary.status === 'inconclusive') ||
        summary.status === 'completed')) {
    throw new Error('저장 결과의 schema가 현재 화면과 일치하지 않습니다.');
  }
  return summary;
}

function integrationEvenSize(value, name) {
  const number = integrationNumber(value, name, 8, 32, true);
  if (number % 2) throw new Error(`${name} 값은 짝수여야 합니다.`);
  return number;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {INTEGRATION, EDITABLE_KINDS, integrationNumber, integrationAxisMs,
    integrationSource, integrationSelectableLenses, integrationCcm, integrationAnnotationDocument,
    integrationSpec, integrationSummary};
}

if (typeof document !== 'undefined') (() => {
  const $ = id => document.getElementById(id);
  const make = (tag, value, className = '') => {
    const node = document.createElement(tag);
    node.textContent = String(value);
    if (className) node.className = className;
    return node;
  };
  const val = id => $(id).value;
  const fmt = value => typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString('ko-KR', {maximumFractionDigits: 4}) : '—';
  const templates = {}, catalogs = {lenses: [], sensors: [], hdrLenses: [], hdrSensors: [], lfmLenses: [], lfmSensors: [],
    qualityJobs: [], ispLensJobs: [], datasetLensJobs: [], cohortJobs: [], jointQualityJobs: [],
    adaptiveRawJobs: [], detectorIspJobs: [], pwmHdrLfmJobs: []};
  let datasetLensTemplate = null, loadedDatasetQualityTemplate = null, datasetQualityLimitation = '';
  let datasetLabeledTemplate = null, datasetAnnotationTemplate = null;
  let lensLocalTemplate = null, lensBoundedTemplate = null;
  let lensCommonSceneTemplate = null;
  let lensDenseSceneTemplate = null;
  let loadedLensCentralTemplate = null, lensCentralLimitation = '';
  let hdrBoundedTemplate = null, lfmBoundedTemplate = null, lfmTimedTemplate = null;
  let loadedHdrCentralTemplate = null, loadedLfmCentralTemplate = null;
  let hdrCentralLimitation = '', lfmCentralLimitation = '';
  let detectorAvailability = {status: 'operator_trust_required'};
  let token = '', active = null, timer = null, epoch = 0;
  const submitting = new Set();

  async function api(path, payload) {
    const response = await fetch('/api/v1/' + path, payload === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Platform-Token': token},
      body: JSON.stringify(payload),
    });
    const body = await response.text();
    let data;
    try { data = JSON.parse(body); } catch { data = null; }
    if (!response.ok) {
      const detail = data?.error || data?.detail;
      throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `HTTP ${response.status}`);
    }
    if (data === null || typeof data !== 'object') throw new Error('서버가 JSON 결과를 반환하지 않았습니다.');
    return data;
  }
  function error(message) { $('error').hidden = false; $('error').textContent = message?.message || String(message); }
  function clearError() { $('error').hidden = true; $('error').textContent = ''; }
  function option(value, label, disabled = false) {
    const node = make('option', label); node.value = value; node.disabled = disabled; return node;
  }
  function fillSensors(id, models, rows = catalogs.sensors) {
    const target = $(id), previous = target.value;
    target.replaceChildren(option('', '저장 센서 run 선택'), ...rows.map(row => {
      const ready = row.status === 'simulated' && !!row.run_id && models.includes(row.model);
      return option(row.run_id || '', [row.manufacturer, row.device_name, row.report_code, row.model]
        .filter(Boolean).join(' · ') + (ready ? '' : ' · 이 작업에서 사용 불가'), !ready);
    }));
    if (rows.some(row => row.run_id === previous)) target.value = previous;
  }
  function fillLenses(id, rows) {
    const target = $(id), previous = target.value;
    const filtered = integrationSelectableLenses(rows, val(`${id}-query`), $(`${id}-all`).checked);
    target.replaceChildren(option('', '렌즈 revision 선택'), ...filtered.map(row =>
      option(row.revision_id, [row.manufacturer, row.name || row.simulation_id, row.configuration]
        .filter(Boolean).join(' · ') + (row.preview_status === 'ready' ? '' : ` · ${row.reason || '미지원'}`),
      row.preview_status !== 'ready')));
    if (filtered.some(row => row.revision_id === previous)) target.value = previous;
    $(`${id}-count`).textContent = `표시 ${filtered.length}건 · 실행 가능 ${rows.filter(row => row.preview_status === 'ready').length}건 · 전체 ${rows.length}건`;
  }
  function fillQualitySources() {
    const datasetLens = val('dataset-source-kind') !== 'saved_sensor_quality';
    for (const [kind, sourceList] of [
      ['dataset', datasetLens ? catalogs.datasetLensJobs : catalogs.qualityJobs],
      ['isp', catalogs.ispLensJobs],
    ]) {
      const target = $(`${kind}-source`), previous = target.value;
      target.replaceChildren(option('', kind === 'isp' || datasetLens ? '완료한 렌즈 × 센서 RAW 선택' : '완료한 화질 작업 선택'), ...sourceList.map(row =>
        option(row.id, `${row.name || row.id} · ${row.status}`, row.status !== 'succeeded')));
      if (sourceList.some(row => row.id === previous)) target.value = previous;
    }
  }
  function fillAdvancedSources() {
    for (const [id, sources, placeholder] of [
      ['pwmHdr-source', catalogs.pwmHdrLfmJobs, '완료된 결합 PWM/LFM 선택'],
      ['adaptive-source', catalogs.adaptiveRawJobs, '10×10 이상 완료 RAW 선택'],
      ['detector-raw', catalogs.adaptiveRawJobs, '완료 RAW 선택'],
      ['detector-isp', catalogs.detectorIspJobs, '완료 ISP RGB 선택'],
    ]) {
      const target = $(id), previous = target.value;
      target.replaceChildren(option('', placeholder), ...sources.map(row =>
        option(row.id, `${row.name || row.id} · ${row.id}`, row.status !== 'succeeded')));
      if (sources.some(row => row.id === previous && row.status === 'succeeded')) target.value = previous;
    }
  }
  function fillCohortSources() {
    const previous = new Map([...$('cohort-sources').querySelectorAll('.cohort-source')].map(row =>
      [row.dataset.id, {checked: row.querySelector('input[type="checkbox"]').checked,
        split: row.querySelector('select').value}]));
    const rows = catalogs.cohortJobs.map(source => {
      const row = document.createElement('div'); row.className = 'cohort-source'; row.dataset.id = source.id;
      const label = document.createElement('label'); label.className = 'check';
      const check = document.createElement('input'); check.type = 'checkbox'; check.checked = previous.get(source.id)?.checked || false;
      label.append(check, make('span', `${source.name || source.id} · ${source.id}`));
      const split = document.createElement('select'); split.setAttribute('aria-label', `${source.name || source.id} split`);
      for (const value of ['train', 'validation', 'test']) split.append(option(value, value));
      split.value = previous.get(source.id)?.split || 'train';
      row.append(label, split); return row;
    });
    $('cohort-sources').replaceChildren(...rows);
    if (!rows.length) $('cohort-sources').append(make('p', '완료된 Lens × Sensor RAW 작업이 없습니다. 01에서 작업을 만든 뒤 목록을 갱신하세요.', 'note'));
    updateForms();
  }
  function fillJointQualitySources() {
    for (const slot of ['L1S1', 'L1S2', 'L2S1', 'L2S2']) {
      const target = $(`jointQuality-${slot}`), previous = target.value;
      target.replaceChildren(option('', '완료한 결합 RAW 선택'), ...catalogs.jointQualityJobs.map(row =>
        option(row.id, `${row.name || row.id} · ${String(row.lens_revision_id || '').slice(0, 8)} × ` +
          `${String(row.sensor_run_id || '').slice(0, 8)} · FOV ${fmt(row.horizontal_fov_deg)}°`,
        row.status !== 'succeeded')));
      if (catalogs.jointQualityJobs.some(row => row.id === previous)) target.value = previous;
    }
    updateForms();
  }
  async function refreshLensSources() {
    const [isp, cohort, dataset, jointQuality, adaptive, detector, pwmHdr] = await Promise.all([
      api(`${INTEGRATION.isp.path}/template`), api(`${INTEGRATION.cohort.path}/template`),
      api('retained-raw-datasets/template?source_kind=lens_sensor_raw'),
      api(`${INTEGRATION.jointQuality.path}/template`),
      api(`${INTEGRATION.adaptive.path}/template`),
      api(`${INTEGRATION.detector.path}/template`),
      api(`${INTEGRATION.pwmHdr.path}/template`),
    ]);
    if (isp.specification?.schema !== INTEGRATION.isp.schema || !Array.isArray(isp.source_lens_sensor_jobs) ||
        cohort.specification?.schema !== INTEGRATION.cohort.schema || !Array.isArray(cohort.source_jobs) ||
        dataset.specification?.schema !== DATASET_LENS_SCHEMA || !Array.isArray(dataset.source_jobs) ||
        jointQuality.specification?.schema !== INTEGRATION.jointQuality.schema ||
        !Array.isArray(jointQuality.source_jobs) ||
        adaptive.specification?.schema !== INTEGRATION.adaptive.schema ||
        !Array.isArray(adaptive.source_lens_sensor_jobs) ||
        detector.specification?.schema !== INTEGRATION.detector.schema ||
        !Array.isArray(detector.source_lens_isp_jobs) ||
        pwmHdr.specification?.schema !== INTEGRATION.pwmHdr.schema ||
        !Array.isArray(pwmHdr.source_lfm_jobs)) {
      throw new Error('완료 Lens RAW 목록의 서버 계약이 바뀌었습니다.');
    }
    catalogs.ispLensJobs = isp.source_lens_sensor_jobs;
    catalogs.cohortJobs = cohort.source_jobs;
    catalogs.datasetLensJobs = dataset.source_jobs;
    catalogs.jointQualityJobs = jointQuality.source_jobs;
    catalogs.adaptiveRawJobs = adaptive.source_lens_sensor_jobs;
    catalogs.detectorIspJobs = detector.source_lens_isp_jobs;
    catalogs.pwmHdrLfmJobs = pwmHdr.source_lfm_jobs;
    detectorAvailability = detector.availability || {status: 'operator_trust_required'};
    templates.detector = detector.specification;
    $('detector-weights').value = detector.specification.model_weights_sha256 || '';
    $('detector-limits').textContent = detector.limitation + (detectorAvailability.status ===
      'ready_for_development_inference' ? '' :
      ` 개발 실행 잠김: ${detectorAvailability.reason || '운영자의 가중치 SHA 신뢰 선언이 필요합니다.'}`);
    datasetLensTemplate = dataset;
    fillQualitySources(); fillCohortSources(); fillJointQualitySources(); fillAdvancedSources();
    updateForms();
  }
  function setDefaults(kind) {
    const spec = templates[kind];
    if (!spec) return;
    $(`${kind}-name`).value = spec.name;
    if (kind === 'lens') {
      $('lens-scope').value = spec.schema === LOCAL_LENS_SCHEMA ? 'local_field_psf' :
        spec.schema === DENSE_COMMON_SCENE_LENS_SCHEMA ? 'central_bounded_dense_common_scene' :
        spec.schema === COMMON_SCENE_LENS_SCHEMA ? 'central_bounded_common_scene' :
        spec.schema === BOUNDED_LENS_SCHEMA ? 'central_bounded' : 'central';
      $('lens-local-fields').hidden = spec.schema !== LOCAL_LENS_SCHEMA;
      $('lens-field-index').required = spec.schema === LOCAL_LENS_SCHEMA;
      $('lens-field-azimuth').required = spec.schema === LOCAL_LENS_SCHEMA;
      const commonScene = [COMMON_SCENE_LENS_SCHEMA, DENSE_COMMON_SCENE_LENS_SCHEMA].includes(spec.schema);
      $('lens-common-scene-fields').hidden = !commonScene;
      $('lens-common-fov').required = commonScene;
      $('lens-dense-scene-fields').hidden = spec.schema !== DENSE_COMMON_SCENE_LENS_SCHEMA;
      $('lens-scene-samples-rows').required = spec.schema === DENSE_COMMON_SCENE_LENS_SCHEMA;
      $('lens-scene-samples-cols').required = spec.schema === DENSE_COMMON_SCENE_LENS_SCHEMA;
      if (commonScene) {
        $('lens-common-fov').value = spec.scene_horizontal_fov_deg;
      }
      if (spec.schema === DENSE_COMMON_SCENE_LENS_SCHEMA) {
        $('lens-scene-samples-rows').value = spec.scene_samples_row_col[0];
        $('lens-scene-samples-cols').value = spec.scene_samples_row_col[1];
      }
      if (spec.schema === LOCAL_LENS_SCHEMA) {
        $('lens-field-index').value = spec.field_psf_index;
        $('lens-field-azimuth').value = spec.field_azimuth_deg;
      }
      $('lens-scene').value = spec.scene_kind;
      $('lens-rows').value = spec.sensor_size[0]; $('lens-cols').value = spec.sensor_size[1];
      $('lens-luminance').value = spec.luminance_cd_m2;
      $('lens-exposure').value = spec.exposure_s * 1000;
      $('lens-noise').value = spec.noise_flag; $('lens-seed').value = spec.seed;
    } else if (kind === 'hdr') {
      $('hdr-scope').value = spec.schema === BOUNDED_HDR_SCHEMA ? 'central_bounded' : 'central';
      $('hdr-scene').value = spec.scene_kind;
      $('hdr-rows').value = spec.sensor_size[0]; $('hdr-cols').value = spec.sensor_size[1];
      $('hdr-exposures').value = spec.exposure_s.map(item => item * 1000).join(', ');
      $('hdr-luminance').value = spec.luminance_cd_m2; $('hdr-seed').value = spec.seed;
    } else if (kind === 'lfm') {
      $('lfm-scope').value = spec.schema === TIMED_LFM_SCHEMA ? 'central_bounded_timed' :
        spec.schema === BOUNDED_LFM_SCHEMA ? 'central_bounded' : 'central';
      $('lfm-timing-fields').hidden = spec.schema !== TIMED_LFM_SCHEMA;
      $('lfm-timing-sha').required = spec.schema === TIMED_LFM_SCHEMA;
      $('lfm-rows').value = spec.sensor_size[0]; $('lfm-cols').value = spec.sensor_size[1];
      const fields = [
        ['exposuresMs', '노출 후보 (ms)', spec.exposure_s.map(item => item * 1000).join(', ')],
        ['periodMs', 'PWM 주기 (ms)', spec.pwm_period_s * 1000],
        ['duty', 'Duty cycle (0–1)', spec.duty_cycle],
        ['offRatio', 'Off/On 광량 비 (0–1)', spec.off_to_on_ratio],
        ['onLuminance', 'On 휘도 (cd/m²)', spec.on_luminance_cd_m2],
        ['seed', '난수 seed', spec.seed],
      ];
      $('lfm-fields').replaceChildren(...fields.map(([id, label, initial]) => {
        const wrap = document.createElement('div');
        const caption = make('label', label); caption.htmlFor = `lfm-${id}`;
        const input = document.createElement('input'); input.id = `lfm-${id}`;
        input.type = id === 'exposuresMs' ? 'text' : 'number';
        input.required = true; input.value = initial;
        if (input.type === 'number') input.step = id === 'seed' ? '1' : 'any';
        wrap.append(caption, input); return wrap;
      }));
    } else if (kind === 'isp') {
      const profile = spec.profile;
      if (profile?.schema !== 'camera_isp_profile_v2' ||
          profile.spatial_lsc?.mode !== 'bypass' || profile.raw_denoise?.mode !== 'bypass') {
        throw new Error('저장 RAW ISP template의 bypass 계약이 현재 화면과 다릅니다.');
      }
      for (const [channel, index] of [['r', 0], ['g', 1], ['b', 2]]) {
        $(`isp-wb-${channel}`).value = profile.white_balance.gains_rgb[index];
      }
      $('isp-ccm').value = profile.ccm.matrix_rgb.flat().join(', ');
      $('isp-tone').value = profile.tone.curve;
      $('isp-sharpen').checked = profile.linear_sharpen.mode !== 'bypass';
      $('isp-sharpen-amount').value = profile.linear_sharpen.amount;
    } else if (kind === 'dataset') {
      $('dataset-split').value = spec.split;
    } else if (kind === 'detector') {
      $('detector-labels').value = spec.target_labels.join(', ');
      $('detector-score').value = spec.score_threshold;
      $('detector-weights').value = spec.model_weights_sha256 || '';
    }
  }
  function formInput(kind) {
    if (kind === 'lens') return {name: val('lens-name'), lens: val('lens-revision'), sensor: val('lens-sensor'),
      scope: val('lens-scope') || 'central', fieldIndex: val('lens-field-index'),
      fieldAzimuth: val('lens-field-azimuth'),
      sceneHorizontalFovDeg: val('lens-common-fov'),
      sceneSamplesRows: val('lens-scene-samples-rows'),
      sceneSamplesCols: val('lens-scene-samples-cols'),
      scene: val('lens-scene'), rows: val('lens-rows'), cols: val('lens-cols'), luminance: val('lens-luminance'),
      exposureMs: val('lens-exposure'), noise: val('lens-noise'), seed: val('lens-seed'), ack: $('lens-ack').checked};
    if (kind === 'hdr') return {name: val('hdr-name'), lens: val('hdr-revision'), sensor: val('hdr-sensor'),
      scope: val('hdr-scope'),
      scene: val('hdr-scene'), rows: val('hdr-rows'), cols: val('hdr-cols'), exposuresMs: val('hdr-exposures'),
      luminance: val('hdr-luminance'), seed: val('hdr-seed'),
      ack: $('hdr-ack').checked};
    if (kind === 'lfm') return {name: val('lfm-name'), lens: val('lfm-revision'), sensor: val('lfm-sensor'),
      scope: val('lfm-scope'),
      rows: val('lfm-rows'), cols: val('lfm-cols'),
      timingModeSha256: val('lfm-timing-sha'),
      exposuresMs: val('lfm-exposuresMs'), periodMs: val('lfm-periodMs'), duty: val('lfm-duty'),
      offRatio: val('lfm-offRatio'), onLuminance: val('lfm-onLuminance'), seed: val('lfm-seed'),
      ack: $('lfm-ack').checked};
    if (kind === 'pwmHdr') return {name: val('pwmHdr-name'), source: val('pwmHdr-source'),
      ack: $('pwmHdr-ack').checked};
    if (kind === 'isp') return {name: val('isp-name'), source: val('isp-source'), wbR: val('isp-wb-r'), wbG: val('isp-wb-g'),
      wbB: val('isp-wb-b'), ccm: val('isp-ccm'), tone: val('isp-tone'),
      sharpen: $('isp-sharpen').checked, sharpenAmount: val('isp-sharpen-amount'),
      ack: $('isp-ack').checked};
    if (kind === 'dataset') return {name: val('dataset-name'), source: val('dataset-source'), sourceKind: val('dataset-source-kind'),
      report: val('dataset-report'), case: val('dataset-case'), split: val('dataset-split'),
      labelProvider: val('dataset-label-provider'), labelSet: val('dataset-label-set'),
      labelMethod: val('dataset-label-method'), labelCoverage: val('dataset-label-coverage'),
      labelObjects: val('dataset-label-objects'),
      ack: $('dataset-ack').checked};
    if (kind === 'adaptive') return {name: val('adaptive-name'), source: val('adaptive-source'),
      ack: $('adaptive-ack').checked};
    if (kind === 'detector') return {name: val('detector-name'), raw: val('detector-raw'),
      isp: val('detector-isp'), labels: val('detector-labels'), score: val('detector-score'),
      ack: $('detector-ack').checked};
    if (kind === 'cohort') return {name: val('cohort-name'), sources: [...$('cohort-sources').querySelectorAll('.cohort-source')]
      .filter(row => row.querySelector('input[type="checkbox"]').checked)
      .map(row => ({id: row.dataset.id, split: row.querySelector('select').value})), ack: $('cohort-ack').checked};
    if (kind === 'jointQuality') return {name: val('jointQuality-name'), sourceJobs: Object.fromEntries(
      ['L1S1', 'L1S2', 'L2S1', 'L2S2'].map(slot => [slot, val(`jointQuality-${slot}`)])),
      ack: $('jointQuality-ack').checked};
    return {};
  }
  function updateForms() {
    $('cohort-count').textContent = `선택 ${[...$('cohort-sources').querySelectorAll('.cohort-source input[type="checkbox"]')].filter(item => item.checked).length}개`;
    for (const kind of EDITABLE_KINDS) {
      try {
        const input = formInput(kind);
        if (kind === 'detector' && detectorAvailability.status !== 'ready_for_development_inference') {
          throw new Error('검출기 개발 실행에 운영자 가중치 신뢰 선언이 필요합니다.');
        }
        if (kind === 'dataset' && input.sourceKind === 'lens_sensor_raw_labeled') {
          if (!input.name.trim() || input.name.trim().length > 160 || !input.ack ||
              !catalogs.datasetLensJobs.some(row => row.id === input.source && row.status === 'succeeded') ||
              !['train', 'validation', 'test'].includes(input.split)) {
            throw new Error('라벨 데이터셋 입력과 출처를 확인하세요.');
          }
          integrationAnnotationDocument(datasetAnnotationTemplate, input);
        } else integrationSpec(kind, templates[kind], catalogs, input);
        $(`${kind}-submit`).disabled = submitting.has(kind);
      } catch { $(`${kind}-submit`).disabled = true; }
    }
  }
  const selectedQuality = {dataset: null};
  const sourceRequest = {dataset: 0};
  async function refreshQualityCases(kind) {
    if (kind !== 'dataset') throw new Error('저장 화질 출처 종류가 잘못됐습니다.');
    const request = ++sourceRequest[kind];
    const id = val(`${kind}-source`);
    selectedQuality[kind] = null;
    datasetAnnotationTemplate = null;
    $('dataset-labeled-shape').textContent = '';
    $(`${kind}-report`).replaceChildren(option('', '센서 보고서 선택'));
    $(`${kind}-case`).replaceChildren(option('', '장면 선택'));
    if (kind === 'dataset' && val('dataset-source-kind') !== 'saved_sensor_quality') {
      $('dataset-case-fields').hidden = true;
      $('dataset-report').required = false;
      $('dataset-case').required = false;
      if (val('dataset-source-kind') === 'lens_sensor_raw_labeled' && id) {
        const loaded = await api('retained-raw-datasets/template?source_kind=lens_sensor_raw_labeled&source_job_id=' +
          encodeURIComponent(id));
        if (request !== sourceRequest[kind] || val(`${kind}-source`) !== id) return;
        if (loaded.specification?.schema !== DATASET_LABELED_SCHEMA ||
            loaded.annotation_template?.source_binding?.source_lens_sensor_job_id !== id) {
          throw new Error('라벨 template이 선택 RAW와 일치하지 않습니다.');
        }
        datasetAnnotationTemplate = loaded.annotation_template;
        $('dataset-labeled-shape').textContent = `원본 ${loaded.annotation_template.raw_shape.join('×')} RAW`; 
      }
      updateForms();
      return;
    }
    if (kind === 'dataset') {
      $('dataset-case-fields').hidden = false;
      $('dataset-report').required = true;
      $('dataset-case').required = true;
    }
    if (!id) { updateForms(); return; }
    const job = await api(`${INTEGRATION.quality.path}/${encodeURIComponent(id)}`);
    if (request !== sourceRequest[kind] || val(`${kind}-source`) !== id) return;
    const sourceList = catalogs.qualityJobs;
    if (!sourceList.some(item => item.id === id && item.status === 'succeeded')) {
      throw new Error('이 작업 template에 포함된 완료 저장 화질 출처가 아닙니다.');
    }
    const summary = integrationSummary('quality', job);
    if (!summary || !Array.isArray(summary.sensor_reports)) throw new Error('선택한 화질 작업의 저장 결과가 없습니다.');
    selectedQuality[kind] = summary;
    $(`${kind}-report`).replaceChildren(...summary.sensor_reports.map((report, index) =>
      option(index, `${index} · ${report.revision?.report_code || report.revision?.sensor_id || '센서'}`)));
    if (summary.sensor_reports.length) $(`${kind}-report`).value = '0';
    refreshCaseOptions(kind);
  }
  function refreshCaseOptions(kind) {
    const report = selectedQuality[kind]?.sensor_reports?.[Number(val(`${kind}-report`))];
    $(`${kind}-case`).replaceChildren(...(report?.cases || []).map((item, index) =>
      option(index, `${index} · ${item.scene} · seed ${item.frame_seeds?.[0] ?? '—'}`)));
    if (report?.cases?.length) $(`${kind}-case`).value = '0';
    updateForms();
  }
  function cell(label, value) {
    const wrap = document.createElement('div'); const title = make('dt', label), data = make('dd', value);
    wrap.append(title, data); return wrap;
  }
  function renderSummary(kind, summary) {
    const host = $('result-summary'); host.replaceChildren();
    if (!summary) return;
    const grid = document.createElement('dl'); grid.className = 'summary-grid';
    if (kind === 'lens') {
      const report = summary.preview;
      grid.append(cell('렌즈 revision', report.lens_identity?.revision_id || '—'),
        cell('저장 센서 run', report.sensor_revision?.run_id || '—'),
        cell('광학 적용 범위', report.scope === 'approximate_geometric_local_field_psf_raw_preview'
          ? `국소 PSF · index ${report.lens_model?.field_psf_selection?.stored_field_index ?? '—'}` : '중앙 raytrace'),
        cell('원본 정수 RAW', `${report.raw?.shape?.join('×') || '—'} · ${report.raw?.min_dn ?? '—'}–${report.raw?.max_dn ?? '—'} DN`),
        cell('평균 광전자', `${fmt(report.mean_electrons?.mean)} e⁻/pixel`),
        cell('광학 조건', `f/${fmt(report.lens_model?.f_number)} · ${fmt((report.lens_model?.focal_length_m || 0) * 1000)} mm`),
        cell('물리 검증', report.physical_validation));
      if (summary.schema === COMMON_SCENE_LENS_SCHEMA) {
        const pointMap = report.capture?.native_output_angular_grid?.distortion_aware_point_center;
        grid.append(cell('입력 장면 화각', `${fmt(report.scene?.horizontal_fov_deg)}°`),
          cell('native RAW 출력 화각', `${fmt(report.capture?.native_output_angular_grid?.horizontal_fov_deg)}° · 중심 근축 근사`),
          cell('왜곡 반영 픽셀 중심', pointMap?.status === 'model_distortion_aware_pixel_centers_only'
            ? `${pointMap.wavelength_nm?.length || 0}개 파장 · 점 중심 좌표` : '미확립'),
          cell('출력 격자 정합', '미확립 · 픽셀 면적·PSF를 포함한 공통 격자 없음'));
      }
    } else if (kind === 'hdr') {
      const report = summary.report;
      grid.append(cell('렌즈 revision', summary.source_identities?.lens?.revision_id || '—'),
        cell('저장 센서 run', summary.source_identities?.sensor?.run_id || '—'),
        cell('융합 상태', summary.status === 'inconclusive' ? '판정 불가 · 유효 융합 픽셀 없음' : summary.status),
        cell('RAW 브래킷', `${report.captures?.length || 0}개 원본 노출`),
        cell('고정 패턴', 'FPN 0 가정'),
        cell('물리 검증', report.verification?.physical_validation || '—'));
    } else if (kind === 'lfm') {
      const report = summary.report;
      grid.append(cell('렌즈 revision', summary.source_identities?.lens?.revision_id || '—'),
        cell('저장 센서 run', summary.source_identities?.sensor?.run_id || '—'),
        cell('시간 조건', `${report?.cases?.length || 0}개 노출 사례`),
        cell('조명', '선언된 PWM + 일정 광량 대조'),
        cell('물리 검증', report?.verification?.physical_validation || '미확립'));
      for (const [index, item] of (report?.cases || []).entries()) {
        const ratio = item.phase_modulation_fraction_black_relative;
        grid.append(cell(`노출 ${index + 1} · 위상 변동`,
          item.phase_modulation_status === 'eligible' && Number.isFinite(ratio)
            ? `${fmt(ratio * 100)}% · black 상대` : item.phase_modulation_status || '계산 불가'),
        cell(`노출 ${index + 1} · RAW DN 범위`, fmt(item.phase_mean_dn_range)));
      }
    } else if (kind === 'pwmHdr') {
      const total = Number(summary.fused_hdr_pixels || 0) +
        Number(summary.single_bracket_fallback_pixels || 0) + Number(summary.invalid_pixels || 0);
      grid.append(cell('원본 PWM/LFM 작업', summary.source_lfm_job_id || '—'),
        cell('계산 상태', summary.status === 'inconclusive' ? '판정 불가' : summary.status || '—'),
        cell('두 노출 융합', `${fmt(summary.fused_hdr_pixels)} / ${fmt(total)} 픽셀`),
        cell('단일 노출 fallback', `${fmt(summary.single_bracket_fallback_pixels)} 픽셀`),
        cell('무효 픽셀', `${fmt(summary.invalid_pixels)} 픽셀`),
        cell('원본 LFM schema', summary.report?.source_lfm_schema || '—'),
        cell('제품 HDR/LFM 성능', '평가하지 않음'));
    } else if (kind === 'isp') {
      const delta = summary.before_after?.default_vs_candidate;
      const source = summary.source_binding;
      grid.append(cell('Lens × Sensor RAW 작업', source?.source_lens_sensor_job_id || summary.specification?.source_lens_sensor_job_id || '—'),
        cell('원본 정수 RAW', `${summary.raw_format?.cfa || '—'} · ${summary.raw_format?.bit_depth || '—'} bit`),
        cell('원본 값 동일', summary.raw_values_byte_equal === true ? '확인' : '미확인'),
        cell('유효 픽셀 비율', typeof summary.before_after?.valid_pixel_fraction === 'number'
          ? `${fmt(summary.before_after.valid_pixel_fraction * 100)}%` : '—'),
        cell('ISP 차이', delta?.available === true ? `${fmt(delta.mean_absolute_rgb_difference)} 평균 절댓값` : delta?.reason || '계산 불가'),
        cell('변경된 표본 비율', delta?.available === true ? `${fmt(delta.changed_sample_fraction * 100)}%` : '—'),
        cell('출력 tone', summary.candidate_rgb_consumer?.tone_curve || '—'),
        cell('화질 개선·인지 점수', '평가하지 않음'),
        cell('물리 검증', summary.verification?.physical_validation || '미확립'));
    } else if (kind === 'dataset') {
      grid.append(cell('RAW 원본', summary.source_kind === 'lens_sensor_raw' ? '렌즈 × 센서 DB 결합' : '저장 센서 화질'),
        cell('원본 RAW', `${summary.raw_shape?.join('×') || '—'} · ${summary.raw_dtype || '—'}`),
        cell('원본 값 동일', summary.raw_values_byte_equal === true ? '확인' : '미확인'),
        cell('라벨', summary.labels), cell('데이터셋 검사', summary.dataset_validation?.ok ? '통과' : '확인 필요'));
    } else if (kind === 'cohort') {
      grid.append(cell('원본 작업', `${summary.source_job_count ?? '—'}개`),
        cell('보존 RAW 사례', `${summary.case_count ?? '—'}개`),
        cell('라벨', '없음'),
        cell('데이터셋 검사', summary.dataset_validation?.ok ? '통과' : '확인 필요'),
        cell('물리 검증', summary.verification?.physical_validation || '미확립'));
    } else if (kind === 'jointQuality') {
      const comparison = summary.comparison || {};
      const pointMaps = Object.values(comparison.distortion_aware_point_center_map_by_slot || {});
      const pointRoi = comparison.common_550nm_point_center_roi || {};
      const angularEdge = comparison.matched_object_angle_edge_mtf50 || {};
      const sharedEdge = comparison.common_grid_object_angle_edge_mtf50 || {};
      const sourceRatios = Object.values(comparison.source_step_over_min_distorted_step_by_slot || {})
        .filter(Number.isFinite);
      grid.append(cell('Lens × Sensor 사례', `${Object.keys(summary.cases || {}).length}개`),
        cell('비교 계약', '저장 RAW 2×2 · 고정 ISP'),
        cell('입력 장면 화각', comparison.matched_horizontal_fov === true ? '일치' : '불일치'),
        cell('왜곡 포함 픽셀 중심 좌표', pointMaps.length === 4 && pointMaps.every(Boolean) ?
          '4건 모델 좌표 확인 · 픽셀 적분/PSF 격자 아님' : '일부 또는 전체 미확립'),
        cell('550 nm 공통 각도 범위', pointRoi.nonempty === true ?
          '모델 좌표 겹침 · native edge 분석 조건' : '미확립 또는 겹침 없음'),
        cell('입력/출력 최소 각도 간격', sourceRatios.length === 4 ?
          `최대 ${fmt(Math.max(...sourceRatios))}배 · 모델 픽셀 중심 기준` : '미확립'),
        cell('native RAW 출력 격자', comparison.matched_native_output_angular_grid === true ?
          '검증된 동일 격자' : '서로 다름 · cycles/pixel 직접 비교 불가'),
        cell('겹침 영역 각도 edge 선별', angularEdge.available === true ?
          '중앙 모델 진단 가능 · native ROI 면적은 서로 다름' : '표본·ROI·왜곡 조건 미충족'),
        cell('동일 물체각 ROI·격자 edge', sharedEdge.available === true &&
          sharedEdge.angular_roi_area_matched === true && sharedEdge.angular_sample_grid_matched === true ?
          `${sharedEdge.grid?.shape_row_col?.join('×') || '—'} · 기하 좌표 면적/격자 일치 · 픽셀 적분 미검증` :
          (sharedEdge.reasons || []).join(' ') || '미확립'),
        cell('temporal SNR', '독립 반복 RAW 없어 계산 불가'),
        cell('제품 순위', '허용하지 않음'));
      for (const [slot, item] of Object.entries(summary.cases || {})) {
        const metric = item.edge_mtf50;
        if (!metric) continue;
        grid.append(cell(`${slot} · edge MTF50`,
          metric.available === true ? `${fmt(metric.value)} ${metric.unit || 'cycles/pixel'}` :
            metric.reason || '계산 불가'));
        const angleMetric = angularEdge.per_slot?.[slot];
        if (angleMetric?.available === true) grid.append(cell(`${slot} · 겹침 내 각도 edge MTF50`,
          `${fmt(angleMetric.mtf50_cycles_per_degree_object_angle)} cycles/degree`));
        const commonMetric = sharedEdge.per_slot?.[slot];
        if (commonMetric?.available === true) grid.append(cell(`${slot} · 동일 각도 격자 MTF50`,
          `${fmt(commonMetric.mtf50_cycles_per_degree_object_angle)} cycles/degree · 모델 진단`));
      }
      if (angularEdge.available === true) {
        for (const pair of angularEdge.pairs || []) grid.append(cell(
          `${pair.left}→${pair.right} · 각도 edge 차이`,
          `${fmt(pair.right_minus_left_cycles_per_degree_object_angle)} cycles/degree · native ROI 상이 · 선별 진단`));
      } else if (angularEdge.reasons?.length) {
        grid.append(cell('공통 각도 edge 차단 사유', angularEdge.reasons.join(' ')));
      }
      if (comparison.reasons?.length) grid.append(cell('비교 불가 사유', comparison.reasons.join(' ')));
    } else if (kind === 'adaptive') {
      const report = summary.report || {};
      grid.append(cell('원본 RAW 작업', summary.source_binding?.source_lens_sensor_job_id || '—'),
        cell('원본 정수 RAW', summary.raw_content_sha256 || '—'),
        cell('알고리즘', report.method?.id || '—'),
        cell('유효 내부 픽셀', fmt(report.before_after?.valid_pixels)),
        cell('Bilinear 대비 차이', fmt(report.before_after?.mean_absolute_sensor_rgb_difference)),
        cell('화질 개선', '평가하지 않음'));
    } else if (kind === 'detector') {
      const inference = summary.inference || {};
      grid.append(cell('원본 RAW 작업', summary.source_identities?.lens_raw_job_id || '—'),
        cell('저장 ISP 작업', summary.source_identities?.lens_isp_job_id || '—'),
        cell('오프라인 추론', summary.verification?.detector_inference || '미확인'),
        cell('검출 개수', fmt(inference.detections?.length)),
        cell('가중치 SHA-256', summary.specification?.model_weights_sha256 || '—'),
        cell('제품 인지 성능', '평가하지 않음'));
    } else if (kind === 'quality') {
      grid.append(cell('센서 보고서', `${summary.sensor_reports?.length || 0}개`),
        cell('화질·RAW 장면', (summary.specification?.scenes || []).join(', ') || '—'));
    }
    host.append(grid);
    if (kind === 'quality') {
      const link = make('a', '자세한 장면별 결과는 저장 센서 작업실에서 보기');
      link.href = `/saved-sensor-workbench.html?kind=quality&id=${encodeURIComponent(active.id)}`;
      host.append(link);
    }
  }
  function renderArtifacts(job) {
    const host = $('artifacts'); host.replaceChildren();
    const seen = new Set();
    for (const item of job.artifacts || []) {
      if (!/^\/api\/v1\/artifacts\/[0-9a-f]{64}$/.test(item.url || '') || seen.has(item.sha256)) continue;
      seen.add(item.sha256);
      const link = make('a', `${item.name || '산출물'} · ${item.sha256.slice(0, 16)}…`);
      link.href = item.url; link.download = item.name || 'artifact'; host.append(link);
    }
    if (!host.children.length) host.append(make('p', '저장 산출물이 없습니다.', 'note'));
  }
  function renderJob(kind, job) {
    const summary = integrationSummary(kind, job);
    $('result').hidden = false;
    $('result-limits').textContent = summary?.limitation || job.limitation || '작업 상태와 오류를 확인하세요.';
    $('permalink').href = `?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(job.id)}`;
    $('export').href = `/api/v1/${INTEGRATION[kind].path}/${encodeURIComponent(job.id)}/export`;
    $('export').download = kind === 'dataset' ? 'retained-raw-dataset.zip' :
      kind === 'cohort' ? 'lens-sensor-raw-cohort.zip' : `${kind}-job.json`;
    $('export').hidden = job.status !== 'succeeded';
    $('cancel').hidden = !['queued', 'running'].includes(job.status);
    $('provenance').textContent = JSON.stringify(summary || {
      specification: job.specification,
      source_identity: job.source_identity || job.source_identities,
      status: job.status,
      error: job.error,
    }, null, 2);
    renderSummary(kind, summary); renderArtifacts(job);
  }
  async function loadJob(kind, id) {
    if (!INTEGRATION[kind] || !id) throw new Error('작업 종류와 ID를 입력하세요.');
    const mine = ++epoch; clearTimeout(timer); clearError();
    $('open-kind').value = kind; $('open-id').value = id;
    history.replaceState(null, '', `?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`);
    const job = await api(`${INTEGRATION[kind].path}/${encodeURIComponent(id)}`);
    if (mine !== epoch) return;
    if (job.id !== id || job.job_kind !== INTEGRATION[kind].kind) throw new Error('저장 작업 ID와 종류가 일치하지 않습니다.');
    active = {kind, id};
    $('status').textContent = `${INTEGRATION[kind].label} · ${job.status} · ${id}${job.error ? ' · ' + job.error : ''}`;
    renderJob(kind, job);
    if (['queued', 'running'].includes(job.status)) timer = setTimeout(() => loadJob(kind, id).catch(error), 1500);
    else {
      refreshRecent().catch(error);
      if (job.status === 'succeeded' && ['lens', 'lfm', 'isp'].includes(kind)) {
        refreshLensSources().catch(error);
      }
    }
  }
  async function refreshRecent() {
    const entries = await Promise.all(Object.entries(INTEGRATION).map(async ([kind, contract]) => {
      const rows = await api(contract.path);
      if (!Array.isArray(rows)) throw new Error(`${contract.label} 작업 목록 계약이 잘못됐습니다.`);
      return rows.map(item => ({...item, kind}));
    }));
    const rows = entries.flat().sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || ''))).slice(0, 14);
    const host = $('recent'); host.replaceChildren();
    if (!rows.length) { host.append(make('p', '아직 저장된 작업이 없습니다.', 'note')); return; }
    for (const item of rows) {
      const button = make('button', `${INTEGRATION[item.kind].label} · ${item.name || item.id} · ${item.status}`);
      button.type = 'button'; button.className = 'secondary';
      button.addEventListener('click', () => loadJob(item.kind, item.id).catch(error));
      host.append(button);
    }
  }
  async function init() {
    const session = await api('session');
    if (session.schema !== 'camera_platform_v1' || !session.token) throw new Error('Camera Platform 세션을 확인할 수 없습니다.');
    token = session.token;
    const fetched = await Promise.all(EDITABLE_KINDS.map(kind => api(`${INTEGRATION[kind].path}/template`)));
    const [localLens, boundedLens, commonSceneLens, denseSceneLens, boundedHdr, boundedLfm, timedLfm] = await Promise.all([
      api('lens-sensor-raw/template?scope=local_field_psf'),
      api('lens-sensor-raw/template?scope=central_bounded'),
      api('lens-sensor-raw/template?scope=central_bounded_common_scene'),
      api('lens-sensor-raw/template?scope=central_bounded_dense_common_scene'),
      api('lens-sensor-hdr/template?scope=central_bounded'),
      api('lens-sensor-lfm/template?scope=central_bounded'),
      api('lens-sensor-lfm/template?scope=central_bounded_timed'),
    ]);
    if (localLens.specification?.schema !== LOCAL_LENS_SCHEMA ||
        boundedLens.specification?.schema !== BOUNDED_LENS_SCHEMA ||
        commonSceneLens.specification?.schema !== COMMON_SCENE_LENS_SCHEMA ||
        denseSceneLens.specification?.schema !== DENSE_COMMON_SCENE_LENS_SCHEMA ||
        boundedHdr.specification?.schema !== BOUNDED_HDR_SCHEMA ||
        boundedLfm.specification?.schema !== BOUNDED_LFM_SCHEMA ||
        timedLfm.specification?.schema !== TIMED_LFM_SCHEMA) {
      throw new Error('렌즈 RAW·HDR·LFM 분할 PSF 실행 범위의 template 계약이 다릅니다.');
    }
    lensLocalTemplate = localLens;
    lensBoundedTemplate = boundedLens;
    lensCommonSceneTemplate = commonSceneLens;
    lensDenseSceneTemplate = denseSceneLens;
    hdrBoundedTemplate = boundedHdr;
    lfmBoundedTemplate = boundedLfm;
    lfmTimedTemplate = timedLfm;
    const [lensDataset, labeledDataset] = await Promise.all([
      api('retained-raw-datasets/template?source_kind=lens_sensor_raw'),
      api('retained-raw-datasets/template?source_kind=lens_sensor_raw_labeled'),
    ]);
    if (lensDataset.specification?.schema !== DATASET_LENS_SCHEMA || !Array.isArray(lensDataset.source_jobs) ||
        labeledDataset.specification?.schema !== DATASET_LABELED_SCHEMA ||
        !Array.isArray(labeledDataset.source_jobs)) {
      throw new Error('렌즈 × 센서 RAW 데이터셋 template 계약이 다릅니다.');
    }
    datasetLensTemplate = lensDataset;
    datasetLabeledTemplate = labeledDataset;
    const loaded = Object.fromEntries(EDITABLE_KINDS.map((kind, index) => [kind, fetched[index]]));
    for (const [kind, template] of Object.entries(loaded)) {
      if (template.specification?.schema !== INTEGRATION[kind].schema) {
        throw new Error(`${INTEGRATION[kind].label} 입력 계약이 현재 화면과 다릅니다.`);
      }
      templates[kind] = template.specification;
      $(`${kind}-limits`).textContent = template.limitation;
      setDefaults(kind);
    }
    const {lens, hdr, lfm, isp, dataset, cohort, jointQuality, adaptive, detector, pwmHdr} = loaded;
    loadedLensCentralTemplate = templates.lens;
    lensCentralLimitation = lens.limitation;
    loadedHdrCentralTemplate = templates.hdr;
    loadedLfmCentralTemplate = templates.lfm;
    hdrCentralLimitation = hdr.limitation;
    lfmCentralLimitation = lfm.limitation;
    if (!Array.isArray(lens.lens_catalog?.revisions) || !Array.isArray(lens.sensor_catalog) ||
        !Array.isArray(hdr.lens_catalog?.revisions) || !Array.isArray(hdr.sensor_catalog) ||
        !Array.isArray(lfm.lens_catalog?.revisions) || !Array.isArray(lfm.sensor_catalog) ||
        !Array.isArray(isp.source_lens_sensor_jobs) || !Array.isArray(dataset.source_quality_jobs) ||
        !Array.isArray(cohort.source_jobs) || !Array.isArray(jointQuality.source_jobs) ||
        !Array.isArray(adaptive.source_lens_sensor_jobs) ||
        !Array.isArray(detector.source_lens_isp_jobs) ||
        !Array.isArray(pwmHdr.source_lfm_jobs)) {
      throw new Error('DB 자산 또는 저장 작업 목록 계약이 없습니다.');
    }
    catalogs.lenses = lens.lens_catalog.revisions;
    catalogs.sensors = lens.sensor_catalog;
    catalogs.hdrLenses = hdr.lens_catalog.revisions; catalogs.hdrSensors = hdr.sensor_catalog;
    catalogs.lfmLenses = lfm.lens_catalog.revisions; catalogs.lfmSensors = lfm.sensor_catalog;
    catalogs.qualityJobs = dataset.source_quality_jobs;
    catalogs.datasetLensJobs = datasetLensTemplate.source_jobs;
    catalogs.ispLensJobs = isp.source_lens_sensor_jobs;
    catalogs.cohortJobs = cohort.source_jobs;
    catalogs.jointQualityJobs = jointQuality.source_jobs;
    catalogs.adaptiveRawJobs = adaptive.source_lens_sensor_jobs;
    catalogs.detectorIspJobs = detector.source_lens_isp_jobs;
    catalogs.pwmHdrLfmJobs = pwmHdr.source_lfm_jobs;
    detectorAvailability = detector.availability || {status: 'operator_trust_required'};
    $('detector-limits').textContent = detector.limitation + (detectorAvailability.status ===
      'ready_for_development_inference' ? '' :
      ` 개발 실행 잠김: ${detectorAvailability.reason || '운영자의 가중치 SHA 신뢰 선언이 필요합니다.'}`);
    loadedDatasetQualityTemplate = templates.dataset;
    datasetQualityLimitation = dataset.limitation;
    templates.hdr = hdrBoundedTemplate.specification;
    templates.lfm = lfmBoundedTemplate.specification;
    $('hdr-limits').textContent = hdrBoundedTemplate.limitation;
    $('lfm-limits').textContent = lfmBoundedTemplate.limitation;
    setDefaults('hdr'); setDefaults('lfm');
    fillLenses('lens-revision', catalogs.lenses);
    fillLenses('hdr-revision', catalogs.hdrLenses);
    fillLenses('lfm-revision', catalogs.lfmLenses);
    fillSensors('lens-sensor', ['db_estimate_v1']);
    fillSensors('hdr-sensor', ['db_estimate_v1'], catalogs.hdrSensors);
    fillSensors('lfm-sensor', ['db_estimate_v1'], catalogs.lfmSensors);
    fillQualitySources(); fillCohortSources(); fillJointQualitySources(); fillAdvancedSources();
    $('connection').textContent = '저장 DB와 타입별 작업 계약에 연결했습니다.';
    await refreshRecent();
    const query = new URLSearchParams(location.search), kind = query.get('kind'), id = query.get('id');
    if (kind && id) await loadJob(kind, id);
    updateForms();
  }
  for (const kind of EDITABLE_KINDS) {
    const form = $(`${kind}-form`);
    form.addEventListener('input', updateForms); form.addEventListener('change', updateForms);
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (submitting.has(kind)) return;
      submitting.add(kind); updateForms(); clearError();
      try {
        const input = formInput(kind);
        let spec;
        if (kind === 'dataset' && input.sourceKind === 'lens_sensor_raw_labeled') {
          if (!input.ack || !input.name.trim() || !catalogs.datasetLensJobs.some(
            row => row.id === input.source && row.status === 'succeeded')) {
            throw new Error('라벨 데이터셋의 이름·원본 RAW·범위 확인을 입력하세요.');
          }
          const fresh = await api('retained-raw-datasets/template?source_kind=lens_sensor_raw_labeled&source_job_id=' +
            encodeURIComponent(input.source));
          if (fresh.specification?.schema !== DATASET_LABELED_SCHEMA ||
              fresh.annotation_template?.source_binding?.source_lens_sensor_job_id !== input.source) {
            throw new Error('선택한 RAW의 라벨 계약이 달라졌습니다.');
          }
          const document = integrationAnnotationDocument(fresh.annotation_template, input);
          const imported = await api('retained-raw-datasets/annotations', {document});
          if (imported.source_job_id !== input.source ||
              imported.verification !== 'user_supplied_annotation_contract_only' ||
              !/^[0-9a-f]{64}$/.test(imported.artifact?.sha256 || '') ||
              imported.artifact.sha256 !== imported.computed_sha256) {
            throw new Error('등록된 RAW 라벨 artifact의 결속 또는 해시가 잘못됐습니다.');
          }
          spec = integrationSpec(kind, fresh.specification, catalogs,
            {...input, annotationSha256: imported.artifact.sha256});
        } else if (kind === 'adaptive') {
          const fresh = await api(`${INTEGRATION.adaptive.path}/template?source_lens_sensor_job_id=` +
            encodeURIComponent(input.source));
          const source = fresh.selected_source_contract;
          if (fresh.specification?.schema !== INTEGRATION.adaptive.schema ||
              fresh.specification.source_lens_sensor_job_id !== input.source ||
              source?.native_cfa_contract !== 'three_channel_rggb_2x2' ||
              !Array.isArray(source.raw_shape) || source.raw_shape.length !== 2 ||
              source.raw_shape.some(size => !Number.isInteger(size) || size < 10)) {
            throw new Error('Adaptive demosaic는 결속된 RGGB 원본 RAW 10×10 이상이 필요합니다.');
          }
          spec = integrationSpec(kind, fresh.specification, catalogs, input);
        } else if (kind === 'detector') {
          const fresh = await api(`${INTEGRATION.detector.path}/template?source_lens_sensor_job_id=` +
            encodeURIComponent(input.raw) + '&source_lens_sensor_isp_job_id=' +
            encodeURIComponent(input.isp));
          const binding = fresh.image_binding;
          if (fresh.specification?.schema !== INTEGRATION.detector.schema ||
              fresh.availability?.status !== 'ready_for_development_inference' ||
              binding?.lens_raw_job_id !== input.raw || binding?.lens_isp_job_id !== input.isp ||
              binding?.color_encoding !== 'srgb' ||
              !/^[0-9a-f]{64}$/.test(fresh.specification.model_weights_sha256 || '')) {
            throw new Error('선택한 RAW·ISP sRGB 결속 또는 로컬 검출 가중치를 확인하세요.');
          }
          spec = integrationSpec(kind, fresh.specification, catalogs, input);
        } else spec = integrationSpec(kind, templates[kind], catalogs, input);
        $('status').textContent = `${INTEGRATION[kind].label} 입력과 출처 검사 후 제출 중…`;
        const job = await api(INTEGRATION[kind].path, spec);
        if (!job.id || job.job_kind !== INTEGRATION[kind].kind) throw new Error('작업 ID를 받지 못했습니다.');
        await loadJob(kind, job.id);
      } catch (cause) { error(cause); $('status').textContent = '제출 실패 · 조건 또는 서버 오류를 확인하세요.'; }
      finally { submitting.delete(kind); updateForms(); }
    });
  }
  $('dataset-source').addEventListener('change', () => refreshQualityCases('dataset').catch(error));
  $('dataset-report').addEventListener('change', () => refreshCaseOptions('dataset'));
  $('dataset-source-kind').addEventListener('change', () => {
    const sourceKind = val('dataset-source-kind');
    const lens = sourceKind !== 'saved_sensor_quality';
    const labeled = sourceKind === 'lens_sensor_raw_labeled';
    const selected = labeled ? datasetLabeledTemplate : lens ? datasetLensTemplate : null;
    templates.dataset = selected ? selected.specification : loadedDatasetQualityTemplate;
    $('dataset-limits').textContent = selected ? selected.limitation : datasetQualityLimitation;
    $('dataset-labeled-fields').hidden = !labeled;
    $('dataset-ack-text').textContent = labeled ?
      '원본 RAW 좌표의 사용자 제공 라벨이며 진실성·holdout 독립성은 검증되지 않았음을 확인했습니다.' :
      '원본 RAW를 보존한 무라벨 export이며 DNG·인지 점수가 아님을 확인했습니다.';
    setDefaults('dataset');
    fillQualitySources();
    refreshQualityCases('dataset').catch(error);
  });
  $('lens-scope').addEventListener('change', () => {
    const local = val('lens-scope') === 'local_field_psf';
    const bounded = val('lens-scope') === 'central_bounded';
    const commonScene = val('lens-scope') === 'central_bounded_common_scene';
    const denseScene = val('lens-scope') === 'central_bounded_dense_common_scene';
    templates.lens = local ? lensLocalTemplate.specification :
      denseScene ? lensDenseSceneTemplate.specification :
      commonScene ? lensCommonSceneTemplate.specification :
      bounded ? lensBoundedTemplate.specification : loadedLensCentralTemplate;
    $('lens-limits').textContent = local ? lensLocalTemplate.limitation :
      denseScene ? lensDenseSceneTemplate.limitation :
      commonScene ? lensCommonSceneTemplate.limitation :
      bounded ? lensBoundedTemplate.limitation : lensCentralLimitation;
    setDefaults('lens');
    updateForms();
  });
  for (const kind of ['hdr', 'lfm']) {
    $(`${kind}-scope`).addEventListener('change', () => {
      const scope = val(`${kind}-scope`);
      const bounded = scope === 'central_bounded';
      const timed = kind === 'lfm' && scope === 'central_bounded_timed';
      const selected = timed ? lfmTimedTemplate : bounded ?
        (kind === 'hdr' ? hdrBoundedTemplate : lfmBoundedTemplate) : null;
      templates[kind] = selected?.specification ||
        (kind === 'hdr' ? loadedHdrCentralTemplate : loadedLfmCentralTemplate);
      $(`${kind}-limits`).textContent = selected?.limitation ||
        (kind === 'hdr' ? hdrCentralLimitation : lfmCentralLimitation);
      setDefaults(kind);
      updateForms();
    });
  }
  for (const [id, rows] of [
    ['lens-revision', () => catalogs.lenses],
    ['hdr-revision', () => catalogs.hdrLenses],
    ['lfm-revision', () => catalogs.lfmLenses],
  ]) {
    $(`${id}-query`).addEventListener('input', () => { fillLenses(id, rows()); updateForms(); });
    $(`${id}-all`).addEventListener('change', () => { fillLenses(id, rows()); updateForms(); });
  }
  $('cohort-refresh').addEventListener('click', () => refreshLensSources().catch(error));
  $('refresh-list').addEventListener('click', () => { refreshRecent().catch(error); refreshLensSources().catch(error); });
  $('open-form').addEventListener('submit', event => {
    event.preventDefault(); loadJob(val('open-kind'), val('open-id').trim()).catch(error);
  });
  $('cancel').addEventListener('click', async () => {
    if (!active) return;
    try {
      await api(`${INTEGRATION[active.kind].path}/${encodeURIComponent(active.id)}/cancel`, {});
      await loadJob(active.kind, active.id);
    } catch (cause) { error(cause); }
  });
  init().catch(cause => {
    $('connection').textContent = '저장 DB 또는 작업 계약 연결에 실패했습니다.';
    error(cause); updateForms();
  });
})();

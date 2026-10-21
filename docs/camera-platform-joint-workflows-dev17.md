# Camera Platform dev17: 결합 RAW에서 ISP·라벨 데이터셋까지

`0.16.2.dev17`은 기존 CameraE2E의 Lens DB revision과 저장 Sensor DB run을 사용해 다음 세 연결을 추가했다. 새 센서 제품 모델이나 실측값을 만들어 낸 것은 아니다. [설치 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev17-py3-none-any.whl), [2×2 RAW·화질 설치본 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev17-installed-proof.json), [ISP·라벨 cohort 설치본 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev17-downstream-installed-proof.json)는 같은 wheel SHA-256 `be46514d19d556657ae439a284f96eaa0eddab97e3ca9c093be7d138b21b0713`에 묶인다. 별도 설치 경로의 1,028개 파일을 wheel과 바이트 비교하고, 격리된 Registry와 child Worker로 Job을 실행했다.

[격리 상태 감사](../reports/camera_platform/integrated-workflows-20260928/joint-dev17-state-audit.json)는 audit event 30개와 저장 artifact 23개의 무결성 검사를 통과했다. 이는 해당 Job 저장소의 정합성 검사이며 물리적 정확도 검증은 아니다.

| 작업 | 이번 설치본에서 확인한 동작 | 해석 한계 |
| --- | --- | --- |
| Lens DB × Sensor DB 정수 RAW v5 | 두 렌즈와 두 센서를 동일 수평 화각·128×128 입력 장면에서 각각 16×16 `int32` RAW로 촬영하고 원본 artifact를 재조회했다. 공통 장면 표본 간격 ÷ 최소 왜곡 반영 픽셀 중심 간격은 네 조합에서 0.901, 0.785, 0.563, 0.491이다. | 출력 픽셀의 유한 면적 적분과 PSF를 포함하는 공통 각도 격자·ROI 재표본화가 없다. 화질 Job은 짝비교 MTF 차이와 제품 순위를 계속 차단한다. 렌즈 투과·상대조도는 unity 가정이다. |
| 재사용 ISP 보정 v2 | 동일 렌즈·센서 revision, PSF, CFA, readout, 기하, D65 입력 조건, 노출·잡음 모드에 대해 하나의 해시 고정 LSC/잡음 보정 문서를 서로 다른 장면·seed의 원본 RAW 두 건에 적용했다. 원본 RAW 바이트 보존을 확인했다. 기존 대상 RAW 하나에 묶인 v1 계약도 유지한다. | 보정 수치와 독립성은 공급자 선언이다. 실제 flat/dark 보정 정확도나 일반적인 고급 ISP 전체를 검증하지 않는다. |
| 사용자 라벨 RAW cohort v2 | 저장된 Lens×Sensor RAW 두 건에 사용자 제공 RAW 좌표 박스를 가져와 전역 class map을 만들고, 원본 RAW·원본 annotation SHA를 유지하는 ZIP/manifest를 SDK→Worker→export로 생성했다. 동일 RAW와 동일 합성 장면 계열이 다른 split에 들어가는 것은 차단한다. | 라벨 진실성·분할 독립성은 검증하지 않았다. 두 16×16 합성 입력은 학습용 대표 데이터셋이나 mAP 근거가 아니다. 검출기는 실행하지 않았다. |
| 저장 PWM/LFM → HDR fusion | 같은 `dev17` 설치본의 별도 child Worker 실행에서 2노출×4위상×8×8 원본 RAW 해시가 fusion 결과까지 유지됐다. [시간 작업 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev17-temporal-installed-proof.json)에 dual fusion 4, 단일 bracket fallback 112, invalid 140을 기록했다. | 최종 상태는 `inconclusive`다. 20 µs 행 시간과 PWM 입력은 작성한 조건이며 특정 센서의 HDR/LFM 제품 모드 성능이 아니다. |

SDK에서는 `lens_sensor_raw_template(scope="central_bounded_dense_common_scene")`의 v5 specification에 `scene_horizontal_fov_deg`와 `scene_samples_row_col`을 명시해 제출한다. 동일 모델 조건의 RAW에 보정을 적용하려면 `lens_sensor_calibrated_isp_template(source_lens_sensor_job_id=...)`가 반환하는 `expected_reusable_calibration_binding`과 v2 보정 문서를 사용한다. 라벨 cohort는 각 RAW에 대해 `retained_raw_dataset_template(source_kind="lens_sensor_raw_labeled", source_job_id=...)`의 annotation template을 채워 `import_retained_raw_annotation()`으로 등록한 뒤, `lens_sensor_raw_cohort_template()["annotated_specification"]`에 RAW Job ID, split, annotation SHA를 넣어 제출한다. 모든 결과는 기존 `wait(..., expected_kind=...)`와 각 Job 조회·export 경로에서 재조회한다.

기존 [dev16](camera-platform-joint-workflows-dev16.md)의 저장 PWM/LFM RAW→HDR fusion 경로를 이번 wheel에서도 재실행했다. 이는 선언한 정적 D65/PWM 조건의 제한된 계산이다. 현재 두 저장 센서 run의 scenario에는 `integration_time`과 `noise_flag`가 있지만 line time, HDR readout mode, LFM exposure schedule은 없다. 따라서 현재 코드의 20 µs 행 시간과 synthetic PWM 조건을 특정 제품 모드의 성능으로 해석하면 안 된다.

다음 통합 경계는 세 가지다. 첫째, 센서 mode revision에 행·프레임 시간, 다중 노출 순서, DCG 및 LFM 제어 조건과 출처를 넣어 실제 모드 계약으로 실행한다. 둘째, Lens PSF와 유한 픽셀 적분을 포함한 공통 출력 각도 격자·ROI를 만들어 같은 물체의 공간 성능을 비교한다. 셋째, 대표 크기의 독립 라벨 장면과 평가 모델을 연결해 인지 지표를 산출한다. 이 경계가 충족되기 전에는 HDR/LFM 제품 성능, Lens×Sensor 제품 순위, 범용 ISP 또는 인지 성능을 주장하지 않는다.

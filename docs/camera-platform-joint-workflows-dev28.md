# Camera Platform dev28: MTF footprint 진단과 저장 LFM 전 위상 교차 검사

현재 개발판은 `0.16.2.dev28`이다. [dev27의 DB→OPD 입력 경계·등록 RAW ISP 검증](camera-platform-joint-workflows-dev27.md)을 유지하면서, 제품 판단을 막고 있던 화질·시간 항목을 더 직접적으로 검사했다. 이 문서는 모델 내부 진단의 범위를 설명하며 기존 기본 8770 서버가 dev28로 교체됐다는 기록이 아니다.

## 2×2 Lens×Sensor 화질: 같은 각도 영역에서도 셀 크기는 다르다

기존 공통 격자 MTF는 네 ISP 출력을 같은 550 nm 물체각 영역·격자로 재표본화한다. 이는 과거 **서로 다른 native ROI 면적** 문제를 일부 해결했지만, RAW에 이미 들어간 native 픽셀 적분과 파장별 PSF footprint를 같게 만들지 않는다. 새 `model_550nm_point_cell_footprint`는 각 카메라의 검증된 기하 픽셀 중심 지도에서 중앙 차분 Jacobian을 계산해 셀 면적과 행·열 방향 각도 폭의 min/median/max를 보존한다. 지도 누락·비유한 좌표·접힌 셀은 진단과 공통 격자 MTF를 거부한다.

[dev28 설치본 2×2 화질 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev28-quality-installed-proof.json)는 동일한 16×16 중앙 물체각 격자를 사용했다. 네 카메라의 중앙 기하 셀 면적 중앙값은 `1.0833e-6`, `1.4265e-6`, `2.7914e-6`, `3.6758e-6 deg²`로 최대/최소 `3.393배`이고, 행·열 폭 비율은 각각 `1.842배`다. 이는 **기하 중심 셀 proxy**의 차이이며 실제 수광 footprint나 측정된 MTF가 아니다. `pixel_integrated_effective_area_matched=false`, `product_ranking_allowed=false`를 유지한다. [정의와 다음 구현 입력](camera-platform-common-grid-footprint.md)을 별도로 기록했다.

## 저장 원본 LFM: 2노출×4위상 전하 적분

새 `load_verified_lfm_artifact()`는 신뢰할 보고서 SHA-256에서 NPZ·여덟 원본 RAW phase hash·seed·행 노출 창을 다시 묶는다. `crosscheck_pwm_four_phase_set()`은 선택한 노출의 네 위상 각각에서 저장 평균 전하를 독립 정수 ns PWM overlap 및 기존 `pyisetcam.sensor_compute_temporal()`과 비교한다. 두 노출 모두 계산해 총 8위상을 검사했다. [재현 명령과 원본 binding](camera-platform-lfm-temporal-parity.md)에 입력 해시와 제한을 적었다.

| 저장 노출 | 네 위상의 저장 평균 vs 정수 시간 최대 상대 차이 | 256표본 시간 엔진 최대 상대 차이 | 소스 검사 |
| --- | ---: | ---: | --- |
| 1 ms | `6.61e-10` | `0.0016445` | [소스](../reports/camera_platform/lfm-temporal-parity-20260928/dev28-four-phase-saved-artifact-parity.json), [격리 설치본](../reports/camera_platform/lfm-temporal-parity-20260928/dev28-installed-four-phase-short.json) |
| 4 ms | `3.59e-10` | `0.0010415` | [소스](../reports/camera_platform/lfm-temporal-parity-20260928/dev28-four-phase-long-exposure-parity.json), [격리 설치본](../reports/camera_platform/lfm-temporal-parity-20260928/dev28-installed-four-phase-long.json) |

여덟 위상은 선언한 `0.003` 수치 허용 오차를 충족했다. 원본 LFM 보고서에는 광학 photon 배열의 바이트 해시가 없으므로 재계산한 광학 배열이 원본과 바이트 동일하다고 주장하지 않는다. 시간 엔진의 확률적 정수 RAW 해시도 저장 RAW와 다르다. 따라서 이것은 저장 **평균 전하의 시간 적분 교차 검사**이지 RAW 재생산, HDR fusion, 실제 센서 mode·PWM 환경의 검증이 아니다. 기존 HDR coverage의 `inconclusive`와 `product_hdr_lfm_decision=not_established`를 유지한다.

## 패키지와 남은 관문

[dev28 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev28-py3-none-any.whl)의 SHA-256은 `150ae7838702e7b625fee0a41005137f8a98e92500edcf53828a50c87b78d850`이다. 화질 Job은 wheel에서 별도 설치한 코드와 새 Registry·child Worker로 실행했고 wheel 파일과 설치 파일을 바이트 대조했다. 저장 LFM 8위상은 동일 격리 설치본을 import한 CLI에서도 통과했다. 관련 화질 테스트 14개, LFM 테스트 6개와 변경 파일 Ruff가 통과했다.

다음 물리·제품 관문은 같은 초점·개구·field·파장에서 수렴하는 회절 PSF와 렌즈 광량, 센서의 파장×입사각 응답, **물체각·파장 → ADC 이전 평균 광전자** 전달 응답을 보존해 MTF 유효 footprint를 비교하는 것이다. HDR/LFM은 원본 광학 바이트·센서 die crop·제품 타이밍과 실제 파형·움직임·fusion coverage를 묶어야 한다. 인지 mAP는 독립 정답과 실제 모델 추론이 필요하고, ISP 탐색은 아직 등록 RAW의 제한된 목적함수와 Python service API 범위다. 실측 정확도와 제품 순위는 미확립이다.

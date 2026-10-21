# Lens × Sensor 통합 작업 · `0.16.2.dev15` 개발 검증

`dev15`에서 저장 렌즈·센서 자산을 같은 작업실의 RAW, 제한된 HDR/PWM, ISP, 데이터셋,
인지 **개발 실험**으로 연결했다. [설치 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev15-py3-none-any.whl)을 별도 경로에 설치하고, 별도 child Worker 및 보존 상태에서 실행했다. 작업실은
`/integration-workbench.html`이며 각 작업은 공통 `/api/v1/jobs/{id}`로 대기한 다음
타입별 상세 경로에서 결과와 원본 근거를 조회한다.

| 작업 | 현재 실행 범위 | 설치본 근거 |
| --- | --- | --- |
| 저장 Lens×Sensor RAW | 실제 DB revision 2개 × RGGB 센서 run 2개, 32×32 int32 RAW. v4는 0.12° **입력 장면** 화각을 고정 | [v4 결합 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev15-final-installed-proof.json) |
| 2×2 화질 진단 | 동일 저장 RAW를 고정 ISP로 재처리, 개별 edge MTF50 가능 시 기록. 출력 격자 불일치 시 delta·순위 차단 | 같은 결합 증거 |
| 정적 HDR fusion · PWM/LFM | 중앙 strict PSF 분할 계산, 각각 8×8 브래킷과 가정한 PWM/행 시간 | [시간 작업 증거](../reports/camera_platform/integrated-workflows-20260928/temporal-dev15-final-installed-proof.json) |
| RAW 데이터셋 | v4 원본 int32 mosaic의 정확한 무라벨 ZIP 보존. 별도 사용자 라벨 경로는 [dev14](camera-platform-joint-workflows-dev14.md) 계약 | 결합 증거 |
| 기존 Adaptive Laplacian | 원본 RGGB RAW 10×10 이상에 `pyisetcam.ip` 커널 적용, 같은 RAW의 bilinear와 비교 | 결합 증거 |
| 저장 ISP RGB의 로컬 YOLO11n | 운영자 신뢰 SHA가 설정된 개발 환경에서만 실제 추론. 원본 RAW·ISP RGB·가중치·worker·RGB→BGR 전처리 결속 | [검출기 증거](../reports/camera_platform/integrated-workflows-20260928/detector-dev15-final-installed-proof.json) |

Wheel SHA-256은 `cb8f38d813d3423722482f79e020525b73e108d33849efdabbc9cf7299613acb`이고, 설치된 파일 1,027개의 바이트가 wheel과 일치했다. 관련 Python 회귀 **44개**, JavaScript 작업실 계약, Ruff 검사도 통과했다. 세 설치본 근거는 동일 wheel을 지칭한다. 증거 생성 스크립트는
[결합](../tools/verify_camera_joint_dev15_installed.py),
[시간](../tools/verify_camera_temporal_dev15_installed.py),
[인지](../tools/verify_camera_detector_dev15_installed.py)로 분리돼 있다.

## 공통 입력 장면과 2×2 해석

RAW v4는 `GET /api/v1/lens-sensor-raw/template?scope=central_bounded_common_scene`로 선택한다.
`scene_horizontal_fov_deg`는 **장면 생성의 입력 각도 범위**다. P0014/P0248과
GC50F6 `DEF-2512-801`/Reno12 `DEF-2412-804`의 네 건은 모두 0.12° 입력 장면으로
실행됐다. 하지만 native 출력 화각은 각각 약 0.03179°, 0.03648°, 0.04522°, 0.05189°다.
입력 장면의 중심 각도 표본 간격도 네 조합의 native 출력 픽셀보다 성기다. 저장 결과는
`scene.angular_grid_contract`와 `capture.native_output_angular_grid`를 따로 기록한다.
후자는 중심 근축 추정이며 전 시야의 왜곡 보정 각도 맵이 아니다.

따라서 화각 입력 일치만으로 같은 물체가 출력의 같은 ROI·각도 표본에 놓였다고 볼 수 없다.
품질 작업은 `matched_horizontal_fov=true`를 기록하면서도
`matched_native_output_angular_grid=false`, 수치 paired delta와 제품 순위 `false`를
기록한다. 개별 edge MTF50은 3건만 진단값이 나왔고 1건은 기존 edge extractor가 유효 곡선을
만들지 못했다. 독립 반복 취득이 없으므로 네 건 모두 temporal SNR을 계산하지 않는다.
원본 RAW 네 건과 후속 결과의 해시·작업 ID는 [소스 직접 증거](../reports/camera_platform/integrated-workflows-20260928/common-scene-2x2-source-v4-gc50f6-reno12.json)와 설치본 증거에 남아 있다.

저장 센서 CFA는 preview와 후속 ISP의 계약을 구분한다. 예를 들어 iPhone 14 Pro의 4×4
Quad Bayer RAW는 저장 preview를 만들 수 있지만, 현재 2×2 RGGB 화질·Adaptive ISP에
묵시적으로 넣지 않는다. 이 차단은 의도된 제한이다.

## ISP·인지 사용 범위

`POST /api/v1/lens-sensor-adaptive-demosaic`는 저장된 실제 RGGB 2×2 RAW만 받는다.
서버 template를 `source_lens_sensor_job_id`로 다시 조회하면 원본 크기·CFA·readout과
커널 출처 해시를 확인할 수 있다. `GET /api/v1/lens-sensor-adaptive-demosaic/{id}`의
NPZ에는 원본 정수 RAW, bilinear 및 Adaptive sensor RGB, 비교 마스크가 함께 보존된다.
LSC·RAW denoise·WB·CCM 보정은 이 경로에서 추정하지 않는다. 출력 차이는 화질 개선이 아니다.

`POST /api/v1/lens-sensor-isp-detectors`는 저장 RAW와 그 RAW에서 나온 ISP **sRGB**를
모두 선택해야 한다. 개발 환경의 고정 캐시 `yolo11n.pt`를 실행 가능한 모델로 신뢰한 운영자가
`CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256`에 정확한 소문자 SHA-256을 선언해야
template가 `ready_for_development_inference`를 반환한다. 미설정이면 제출 화면과 서버가
잠긴다. 이 환경변수는 모델 출처 **인증이 아니라 명시적 신뢰 선언**이다. `.pt` 로딩은
실행 가능한 pickle 경로이며, Python socket guard와 Ultralytics offline 설정은 OS 수준
네트워크·파일 격리가 아니다. 시스템 Python 3.9 사용자 패키지의 실제 Torch·Ultralytics·NumPy
버전은 결과에 기록되지만 제출 시 패키지 바이트까지 고정하지 않는다.

설치본에서는 저장 RAW→ISP RGB→YOLO를 SDK 제출·공통 대기·타입별 재조회로 완료했다.
원본 RGB uint8 해시와 모델 호출 전 BGR uint8 해시가 다르고, 이를 각각 결과에 기록한다.
32×32 영상의 검출은 0개였다. 이는 로컬 모델 호출의 실행 증거일 뿐 검출 정확도,
독립 정답, mAP 또는 제품 인지 성능의 증거가 아니다. 수정 전 RGB를 BGR로 전달하지 않았던
과거 개발 추론 결과는 유효한 검출 근거로 승계하지 않는다.

## 아직 남은 통합 경계

현재 HDR은 **정적 두 노출의 제한된 fusion**, LFM은 **선언한 이상적 PWM과 가정한 행 시간**이다.
임의 움직임·PWM·rolling shutter가 함께 작동하는 제품 mode 평가가 아니다. 일반 ISP graph,
Quad Bayer remosaic, 독립 LSC·denoise 보정, 대표 해상도의 인지 데이터셋 평가, detector
가중치 출처 인증과 격리 배포도 아직 없다. 기존 CameraE2E에 관련 계산 자산이 있다는 사실을
이 작은 통합 계약에서 임의 조합이 검증됐다는 뜻으로 해석하지 않는다.

다음 설계 게이트는 순서대로 (1) Lens의 전 시야 출력 각도 맵과 충분히 조밀한 공통 장면 표본으로
비교 가능한 ROI/주파수 기준 확립, (2) 실제 센서 mode와 함께 시간 HDR/LFM의 결합 조건 정의,
(3) 기존 ISP stage별 CFA·보정 출처 계약 확장, (4) 대표 크기의 독립 라벨 세트와 신뢰된
검출 런타임 패키징이다. 그 전까지 이 경로의 물리 검증과 상용 제품 승인 상태는
`not_established`다.

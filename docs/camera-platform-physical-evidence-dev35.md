# Camera Platform dev35: 적응형 Scene 표본화와 PSF 기준 분리

`0.16.2.dev35`는 기존 CameraE2E의 저장 기하 raytrace PSF를 사용하는
Lens×Sensor RAW 경로에 **선택형 수치 격자 실험**을 추가했다. 실제 광학 PSF의
정확도나 제품 MTF를 승인한 릴리스가 아니다. 기본 설치 작업실은 별도
[v0.15 인도 상태](../reports/camera_platform/delivery.json)에 남아 있다.

## 실제 실행에서 확인한 것

[`adaptive_lens_sensor_raw_preview()`](../src/camera_platform/lens_sensor_sampling_convergence.py)는
저장 렌즈 PSF pitch·센서 pitch를 첫 탐색 후보로 삼고, 같은 화각·광량·노출의
연속 경사 에지를 매 격자에서 다시 생성한다. 잡음 없는 평균 광전자, 고정된
반 픽셀 각도 이동 응답, RAW mosaic의 에지 MTF50 수치 proxy를 비교한다.
호출자가 허용 오차와 최대 격자를 지정할 수 있다. 이는 기존 raster의
보간 확대가 아니며, 픽셀 면적이 이미 적분된 PSF 경로에도 적용하지 않는다.

[실제 적응형 실행](../reports/camera_platform/mtf-support-20260928/dev35-real-adaptive-lens-sensor-ladder.json)은
하나의 고정 Lens·Sensor, 0.12° 화각, 4 ms 노출, 16×16 native 출력에서
32²→64²→128²→256² Scene과 두 에지 위치를 각각 계산했다. 마지막
128²→256²에서도 평균 광전자 변화 **12.52%**, 이동 응답 변화 **8.12%**로
선언한 1%·2% 허용치를 넘었다. 시작 pitch 권장 후보도 상한에 걸리고 장면
halo와 전 시야 PSF 재표본화는 검증되지 않아 상태는
`inconclusive_resource_limit`이다. 16×16 Bayer mosaic의 MTF50 proxy는
단계별 양자화가 커서 독립 수렴 근거로 삼지 않는다.

[소형 RAW 실행](../reports/camera_platform/mtf-support-20260928/dev35-executed-small-raw-sampling-evidence.json)은
원본 PSF·실행 광학 convolution·센서 pitch와 실제 mosaic Gaussian/보간/면적
연산자를 따로 기록한다. 광학 PSF는 픽셀 적분 **전** 응답으로 처리해 센서
개구를 두 번 적용하지 않는다. 이 실행의 450 nm 중심 PSF는 64² 장면
창에 약 29.43%만 들어오므로 계산 성공은 충분한 입력 지지의 증거가 아니다.

## 광학 PSF 자체의 기준

저장 Lens PSF는 광선 교점의 bilinear ray histogram이다. 이를 촘촘한
Scene 격자에 선형 보간해 합계 1로 정규화하면 DC는 맞지만 위치·폭·MTF가
맞는다는 보장은 없다. [합성 반례](../reports/camera_platform/mtf-support-20260928/psf_resampling_counterexample.json)는
1 µm 목표 간격에서 중심 위치가 0.35 µm 대신 0.0013 µm가 되고,
비대칭 PSF의 45° 회전에서 원래 배열 내 질량이 58.06%만 남아도
정규화 후 합계는 1이 되는 경우를 보인다.

[동일 광선 재히스토그램 도구](../tools/qualify_lens_psf_sampling.py)는 고정된
P0014 처방과 동공 표본을 다시 추적하여 성공 광선 교점 하나로 원래
0.612884917 µm와 절반인 0.306442459 µm 격자를 각각 만든다.
[중심 550 nm](../reports/camera_platform/mtf-support-20260928/p0014-trace-once-half-pitch.json),
[중심 450 nm](../reports/camera_platform/mtf-support-20260928/p0014-trace-once-central-450-half-pitch.json),
[주변 3번 시야·550 nm·45°](../reports/camera_platform/mtf-support-20260928/p0014-trace-once-field3-550-az45-half-pitch.json)의
원래 격자는 저장 PSF를 재현했지만, 절반 격자의 직접 재히스토그램과
현재 strict 보간은 서로 달랐다. 비교한 세 조건의 L1 분포 차이는
각각 0.586, 1.335, 0.703이다. 이 결과는 선택된 **기하 광선**의
수치 기준이다. 전 파장·시야·방위, 회절·광량·CRA×픽셀 응답을 검증하거나
생산 PSF 구현을 교체하지 않았다.

[1024² 2×2 사전 검사](../reports/camera_platform/mtf-support-20260928/dev35-large-scene-resource-preflight.json)는
원본 분광 Scene, PSF, 패딩 출력과 FFT 임시 배열을 분리 추정해 조합별
3.668–3.994 GiB의 정책 추정치를 기록했다. 이는 peak RSS 상한이 아니다.
현재 macOS 작업 환경에서는 독립 자식 프로세스 메모리 제한을 강제할 수
없어 대형 Job을 제출 전에 거부한다. 화각만 늘린 256² 장면은 원래 각도
표본 간격을 유지하지 못하므로 이 검사를 대신하지 않는다.

## 다른 판정과 설치 경계

HDR/LFM 비교에는 [프레임별 선언 노출 창과 공간·시간 정합 자격](camera-platform-hdr-lfm-mode-readiness.md)을
추가했다. 실측 파형·좌표 변환·readout 교정이 없어 픽셀별 fusion 오차는
여전히 판정 불가다. [검출기 replay 자격](camera-platform-detector-replay-qualification.md)은
실제 오프라인 YOLO Job, ISP RGB 바이트, annotation 출처와 scene split을
재검사하고 서로 다른 annotation cohort의 mAP 합산을 거부한다. 독립적인
정답 장면·훈련/검증 분할이 없어 제품 mAP는 제공하지 않는다. 등록 Factory
Lens와 저장 Sensor revision의 [원본 응답 데이터 요청](camera-platform-native-sensor-data-request.md)은
SDK/API에서도 조회할 수 있지만, 부족한 native EQE를 만들어 주지는 않는다.

[소스 QA](../reports/camera_platform/integrated-workflows-20260928/dev35-source-qa.json)는
관련 회귀 192개, 실제 오프라인 검출기 9개, 선택 광선 재히스토그램 4개
통과와 변경 모듈 Ruff 통과를 기록한다. [dev35 wheel](../reports/camera_platform/integrated-workflows-20260928/dev35-wheel/pyisetcam-0.16.2.dev35-py3-none-any.whl)의
[격리 설치 검증](../reports/camera_platform/integrated-workflows-20260928/dev35-installed-offline.json)은
설치 파일 1,057개와 wheel의 byte 일치, 소스 Python 303개의 checkout 일치,
외부 자산 자동 fetch를 막은 정수 RAW 계산과 저장 Sensor revision의 실행
호환성을 확인했다. 물리 검증, 제품 순위, 상용 배포 승인은 모두 **미확립**이다.

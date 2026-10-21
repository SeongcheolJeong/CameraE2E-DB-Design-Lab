# Camera Platform dev34: 결합 RAW·MTF·ISP 판정의 입력 근거

기존 CameraE2E는 저장된 raytrace PSF를 영상에 적용한다. dev34의 초점은
PSF convolution의 존재 확인이 아니라, 그 결과를 렌즈×센서 제품 판단으로
읽기 전에 필요한 **원본 센서 각도 응답, 충분한 MTF 입력 범위, 독립 평가
근거**를 확인하는 것이다. [dev33](camera-platform-physical-evidence-dev33.md)의
원본 번들 등록과 HDR/LFM 기록 정합을 이어받는다.

## p0014 Lens와 실제 Sensor의 결합

[`native_sensor_data_request()`](../src/camera_platform/native_sensor_readiness.py)는
저장된 Sensor DB revision을 선택해 p0014의 해시 고정 광학 응답과 대조하고,
로컬 QE·FDTD·TCAD 원본의 경로와 SHA-256, 결합 불가 사유, 필요한 새 응답의
축·단위·정규화를 반환한다. 기존 추정 QE나 전체 cell 기준 FDTD 흡수량을
독립 R/Gr/Gb/B 픽셀의 절대 수집 EQE로 바꾸지 않는다.

[실제 자산 감사](../reports/camera_platform/integrated-workflows-20260928/dev33-native-sensor-readiness.json)에서
현재 저장된 simulated Sensor 144개 중 3 µm·네 CFA site 조건을 가진
revision은 10개였으나 모두 estimate다. QE 카탈로그 224개에는 필요한
파장×극각×방위×site 응답 격자가 없다. 생성 TCAD profile은 미측정 proxy이고,
센서별 solver-v3 FDTD 응답은 2.9 µm에서 각각 0/4030 sample 완료,
미검증·전체 2×2 cell 기준 정규화다. p0014 PSF 자체도 550/650 nm의
`PROPOSED` 후보이며 전체 RGB·물리 검증 광학 모델이 아니다.

예시 `DEF-1803-805`는 p0014의 3.0 µm 표본 pitch와 일치하지만,
각도별 수집 EQE와 측정 readout이 없다. `DEF-2206-805`는 2.9 µm이므로
같은 revision에 3.0 µm 응답을 요구하지 않는다. 후자의 요청은 센서 고유
2.9 µm를 보존하고, 결합 전 **일치하는 센서 선택 또는 검증된 광학
재표본화·재생성**을 요구한다. [데이터 요청 계약](camera-platform-native-sensor-data-request.md)에
필요한 원본과 자격 조건을 적었다. 이 근거가 없으므로 실제 p0014×Sensor
물리 RAW와 제품 순위를 생성하지 않았다.

## 기존 2×2 MTF의 동일 각도 간격 재실행 조건

[실행형 사전 검사](../src/camera_platform/lens_sensor_mtf_capture_preflight.py)는
저장된 네 RAW, 품질 Job, PSF·왜곡 원본과 코드 해시를 대조한다. 전역 PSF
반경을 strict 왜곡 역변환과 픽셀 적분의 조건부 범위에 전달하면, 기존
256×256·수평 화각 0.12°와 같은 각도 샘플 간격의 후보 입력은
**1024×1024·수평 화각 0.479997368132°**다. [결과](../reports/camera_platform/mtf-support-20260928/saved-dev29-mtf-recapture-preflight.json)는
조건별 최소 창, 화각, 자원 하한과 차단 사유를 보존한다.

현재 RAW 장면 계약은 각 축 256 sample이 상한이다. 1024 실행의 peak
memory·시간 상한은 검증되지 않았고, 과거 RAW에는 최종 mosaic 적분
연산자가 직접 저장되지 않았다. 같은 해시의 소스와 설정에서 단일 subpixel·
OCL off 경로를 재구성했지만 이를 과거 실행 metadata로 승격하지 않는다.
사전 검사는 새 RAW 제출을 거부했고 물리 MTF 또는 제품 순위를 통과시키지
않았다. [MTF 상세 조건](camera-platform-mtf-support-qualification.md)에
재실행과 경계·표본 수렴 확인 순서를 적었다. 픽셀 면적 차이 자체가 순위
금지 근거는 아니며, 같은 물체각 ROI에 대한 각 시스템의 입력 지지가 핵심이다.

## ISP·인지·HDR 판정 경계

[`source_bound_isp_optimization`](../src/camera_platform/source_bound_isp_optimization.py)의
새 v2 receipt는 tuning/holdout RAW 출처를 기록하고, 보류 입력이 계획 시점에
보이는 **비블라인드 회고 평가**임을 표시한다. 평가 기준을 만족해도
`product_performance.available=false`다. 기존 v1 receipt는 계속 읽힌다.
인지 cohort의 mAP는 작은 합성 fixture의 연결 진단이며 검증된 annotation·
독립 detector replay·제품 장면 분포의 mAP가 아니다. 고급 ISP 전체와
범용 최적화가 제품 수준으로 검증된 상태도 아니다.

HDR/LFM은 dev33의 구조화된 mode·waveform·reference packet으로 내부
일관성을 확인한다. 계측 원본의 독립성, 실제 공간 정합, 기준율의 물리
검증은 아직 없다. 결과는 `inconclusive`이고 제품 성능 주장에 쓰지 않는다.

## 설치 실행의 자산 경계

Spatial RAW에서 광학·QE 계산 후 이미 구한 전자 수를 digital readout에
전달할 때, 더는 사용하지 않는 기본 RGB 필터 파일을 로드하지 않는다.
[`precomputed_raw.py`](../src/camera_platform/precomputed_raw.py)는 기존
CameraE2E readout을 재사용하고 임시 센서를 외부에 노출하지 않는다.
기존 저장 Sensor DB가 해시로 고정한 `pyisetcam/sensor.py` 및
`sensor_capture.py`는 변경하지 않아 저장 revision의 실행 호환성을
유지한다. 이 개선은 숫자 RAW를 만들 때의 설치·운영 결함을 고친 것이며
광학·센서의 물리 정확도를 높였다는 뜻은 아니다.

[dev34 wheel](../reports/camera_platform/integrated-workflows-20260928/dev34-wheel/pyisetcam-0.16.2.dev34-py3-none-any.whl)을
격리 설치했다. [설치본 검증](../reports/camera_platform/integrated-workflows-20260928/dev34-installed-offline.json)은
wheel의 1052개 파일 bytes와 설치 파일의 일치, Python 소스 298개의
wheel·checkout 일치, 기본 자산 fetch를 강제로 금지한 4×64×64 정수 RAW,
저장 Sensor DB revision의 실행 호환성을 확인한다. 관련 소스 회귀는
[178개 통과](../reports/camera_platform/integrated-workflows-20260928/dev34-source-qa.json)했다.
이는 설치·계산 구현의 검증이고 장치 성능 검증은 아니다.

다음 실행 단계는 1024 입력의 resource preflight와 mosaic 연산자 기록,
동일 조건의 새 2×2 RAW·MTF 수렴 검사다. p0014 결합은 위 데이터 요청에
맞는 Sensor 원본이 등록되면 진행할 수 있다. HDR/인지/ISP 제품 지표는
독립된 원본과 평가 절차가 필요하다.

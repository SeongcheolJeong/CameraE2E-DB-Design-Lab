# Native 4사이트·방위각 응답을 RAW/HDR까지 보존

새 Sensor 계약은 `[R, Gr, Gb, B]`의 절대 수집 EQE를 파장·입사각·방위각에 대해
표현합니다. 기존 RGB QE와 상대 각도 응답의 곱을 대체하며, CFA·microlens·흡수·
collection을 추가로 곱하지 않습니다. 첫 구현의 범위는 독립 사이트 응답입니다.
사이트 간 광학·전기 재분배나 FDTD 셀 정규화 응답을 이 계약으로 자동 변환하지 않습니다.

이 문서는 개발 소스 기능과 계산 검증 범위를 설명합니다. 실물 Sensor/Lens 정확도,
고객 환경 설치, 움직임 HDR/DCG 또는 제품 전체의 완성을 의미하지 않습니다.

## 모델과 방향 계약

- Sensor: `camera_native_pixel_response_v1`, 값 축
  `[wavelength, theta, phi, site]`, 사이트 순서 `[R, Gr, Gb, B]`.
- Native 사이트 원점: `site_index_tile=[[0,1],[2,3]]`와 full sensor 원점에 고정합니다.
  crop의 출력 `(row, col)`은
  `tile[(row+crop_y)%2][(col+crop_x)%2]`를 사용합니다.
- 응답 기준면: `sensor_surface_before_pixel_optics`. 값의 단위는
  `electron/incident_pixel_photon`이며, 독립 사이트 계약에서는 `[0,1]`입니다.
- 방향: 광자가 Sensor 안으로 진행하는 +z, 열 증가 +x, 행 증가 +y입니다.
  phi는 +x에서 +y로 증가합니다. 역추적 광선은 실제 진행 방향으로 변환합니다.
- 파장·theta는 공급 범위 밖 입력을 거부합니다. phi는 0을 포함하는 주기 축이며
  중복 360 노드를 받지 않습니다. 비정상 입사 모델은 전체 phi 주기를 명시해야 합니다.
  정상 입사에서는 같은 사이트의 모든 phi 값이 같아야 합니다.

Lens Factory v3는 `camera_lens_directional_response_v1`을 생성합니다. 이 계약에는
기준 field의 결합 방향 벡터와 파장별 조건부 광량 분율이 들어갑니다. 서로 독립적인
theta·phi 분포를 곱해 방향을 만들어내지 않습니다. 가중치는 이미 적분된 광량
분율이므로 추가 `sin(theta)` 또는 입체각 계수를 곱하지 않습니다.

현재 회전은 centered spherical/plane 처방과 radial stop 범위에서만 사용합니다.
기준 field의 전체 진행 벡터를 native 수광 위치의 방위각만큼 회전합니다. On-axis
annulus는 명시적인 방위각 quadrature로 확장합니다. 저장된 한 광선을 annulus 전체의
유일한 실제 방향으로 해석하지 않습니다. 비대칭 광학계의 일반 field 회전은 지원하지 않습니다.

## 실행과 저장

PSF와 RI를 적용한 분광 조도에 결합 방향별 절대 EQE를 적분하고, 파장 적분과
native pixel 면적을 한 번 적용합니다. 결과는 `[H,W,4]` 광전자율입니다.
Temporal/HDR는 이 사이트 축을 시간 적분까지 보존한 뒤 native 사이트를 선택하여
실제 전기 readout에 전달합니다. ISP의 출력 RGB 세 채널과 Sensor의 네 사이트는
서로 다른 축입니다. Bayer RAW의 Gr/Gb를 보존한 상태에서 ISP가 RGB를 생성합니다.

저장되는 근거에는 원본 응답·Lens 방향 표와 SHA256, native 사이트 map, 사이트별
광전자율·전하, row/frame 시각, 실제 integer RAW, RGB 및 parent bundle이 포함됩니다.
등록 서버가 canonical 응답 blob을 생성하며, 제출·Worker·재사용에서 blob과 고정된
모델·요청의 대응을 확인합니다. 누락·손상·축 불일치·효과 중복은 거부합니다.

정적 순차 HDR에는 실제 RAW와 명시적인 전기 calibration만 전달합니다. Simulator의
평균 전하와 포화 truth mask는 HDR 융합이나 sampled RAW prediction의 입력이 아닙니다.
잡음이나 clipping이 있는 RAW를 “노출 두 배이면 코드도 정확히 두 배”라고 해석하지 않습니다.

Validation은 정확한 `[R,G,B]` 또는 `[R,Gr,Gb,B]` 채널 축을 받습니다. Prediction과
reference의 축은 일치해야 합니다. 서로 다른 green을 평균하거나 순서를 자동 변경하지
않습니다. 기존 RGB RAW 분석 결과를 네 사이트 reference로 사용할 수 없으며, 실측
네 사이트 참조에는 해당 원점·시각·적용 조건을 보존한 별도 관측 근거가 필요합니다.

Native 응답은 현재 Spatial·Temporal·순차 HDR 경로에서 실행합니다. Closed-loop
AE/AWB는 네 사이트 피드백 연산자와 근거 보존을 아직 연결하지 않았으므로
제출 전 검사에서 명시적으로 거부합니다.

## 계산 검증 범위

개발 소스 시험에서 다음을 확인합니다.

- 독립적인 면적·파장·노출 적분과 실제 ADC, 서로 다른 Gr/Gb, 네 crop 위상.
- 1배/2배 노출의 포화 전 평균 전하와 실제 RAW 연결.
- 동일 green·방위각 불변 조건에서 기존 RGB 경로의 실제 RAW/RGB와 회귀 일치.
- Prepared temporal capture와 HDR에서도 사이트 축 보존.
- HDR oracle 배열을 오염해도 RAW가 같으면 융합 결과가 같음.
- RGB/네 사이트 reference 간 불일치 거부와 green swap의 개별 잔차 검출.
- 네 사이트 배열 크기, 전체 unique scene 방향 적분량, 저장 artifact 크기의 실행 전 상한.

정확한 시험 파일과 실행 결과는 해당 milestone 보고서에 기록합니다. 계산 경로의
검증과 독립 실물 검증은 별도의 근거로 관리합니다.

## 별도 API/Worker 검증 도구

[`tools/verify_camera_native_pixels.py`](../tools/verify_camera_native_pixels.py)는 기본
설치본 8770과 primary state를 거부합니다. 별도 서버를 준비한 후 실행합니다.

```sh
PYTHONPATH=src .venv-platform/bin/python tools/verify_camera_native_pixels.py \
  --base-url http://127.0.0.1:QA_PORT \
  --expected-state-dir /absolute/path/to/isolated-native-qa \
  --output-dir /absolute/path/to/native-pixel-report
```

도구는 실제 v3 PSF Factory와 같은 처방의 Ray Factory를 실행하고, 두 Factory 근거가
연결된 Lens와 명시적인 합성 Sensor를 등록합니다. 합성 응답에는 서로 다른 green과
작은 방위각 차이가 들어갑니다. 이는 측정된 anisotropy를 주장하지 않습니다.
8×8 분광 입력으로 Spatial RAW와 2ms/4ms HDR RAW를 생성합니다.

작은 독립 계산은 저장된 PSF를 source pixel별로 직접 합산하고 실제 결합 방향의
광량 분율에 합성 EQE 식을 적용합니다. Runtime의 convolution/EQE 함수를 재호출하지
않습니다. 이 계산은 주어진 표의 적용·원점·단위를 검사하며 Lens 표 자체의 독립적인
물리 정확도 검증이 아닙니다. 별도의 Poisson·ADC 계산으로 실제 HDR RAW를 확인하고,
실제 RAW만으로 계산한 inverse-variance HDR 결과도 비교합니다.

`summary.json`은 POST intent, payload hash, 반환 ID와 오류 본문을 저장합니다.
같은 인자로 다시 실행하면 기존 ID를 조회하고 해당 job을 기다립니다. ID를 받지 못한
POST는 불확실하므로 자동 재제출하지 않습니다. 반환 상태와 저장 기록을 대조하여
기존 실행을 복원해야 합니다. 서버 재시작 뒤 같은 인자로 재실행하면 같은 immutable
record와 모든 다운로드 bytes를 다시 확인할 수 있습니다.

결과 폴더에는 full record JSON, SHA256으로 검증한 원본 artifact, 독립 광전자율 NPY,
Spatial/HDR evidence ZIP과 검증 항목이 저장됩니다. 이 도구를 작성했다는 사실만으로
live 실행이나 재시작 검증을 완료했다고 판단하지 않습니다. `summary.json`의 실제
상태와 해당 실행/재시작 기록을 확인해야 합니다.

## 남는 범위

FDTD 셀 재분배·공간 crosstalk 전달, 일반 광학계의 벡터/편광·coherent 전달,
TCAD collection, 실제 센서 각도별 측정과 독립 holdout 검증, native closed-loop AE/AWB,
DCG·움직임 HDR/LFM,
고급 ISP와 다목적 최적화, 설치본 갱신 및 고객 환경 배포가 남아 있습니다.
각도/field 보간과 PSF·수광 응답 분리 근사의 오차, 전체 물리 불확실도도 미확립입니다.

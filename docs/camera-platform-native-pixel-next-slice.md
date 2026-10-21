# 다음 픽셀 확장: native 4개 사이트와 방위각을 전하 계산까지 보존

2026-09-20 읽기 전용 코드 감사에 근거한 **미구현 제안**입니다.
v0.16.0.dev2의 공유 stop transmission 다음 단계이며, 이 문서는 기능 구현이나
수치·실물 검증 완료를 뜻하지 않습니다. 기존 7개 모듈과 4개 실행 모드의 경계를
유지하고 Sensor 모델 및 광학 응답 전달 경로를 확장합니다.

목표는 R/Gr/Gb/B 사이트와 입사 방위각을 모델 등록부터 광전자율, 실제 RAW까지
유지하는 것입니다. UI 필드나 결과 메타데이터만 늘리는 변경은 완료로 인정하지 않습니다.

## 현재 확인한 경로와 제한

- [`physical_estimate.py`](../src/pyisetcam/physical_estimate.py)의
  `physical_optical_response`는 파장·theta·phi 입력을 broadcast하고 마지막 축에
  `[R, Gr, Gb, B]`를 반환합니다. 광학층 흡수와 collection을 포함한 EQE입니다.
  현재 두 green 사이트는 같은 green 재료·채널 설정을 공유합니다.
- 기본 `routing_mode='no_routing_loss'`의 EQE에는 microlens own-site overlap을
  손실로 적용하지 않습니다. 이 모드의 planar EQE는 phi에 의존하지 않습니다.
  방위각과 microlens shift의 영향은 overlap 진단 및 명시적으로 선택한
  `own_site_lower_bound`에 나타납니다. 이를 일반적인 사이트 간 재분배 해법으로
  해석하면 안 됩니다.
- [`spatial.py`](../src/camera_platform/spatial.py)의 `_spectral_sensor`는
  `[RGB, wavelength, theta]` 응답을 적분하고 native CFA를 `[[0,1],[1,2]]`로
  선택합니다. 따라서 Gr/Gb 차이를 표현할 수 없습니다. Lens 가중치에도 phi 축이
  없습니다. [`runtime.py`](../src/camera_platform/runtime.py)의
  `ResponseDBRuntime._parse`와 [`service.py`](../src/camera_platform/service.py)의
  Sensor 등록 검증도 같은 3채널·theta 계약을 요구합니다.
- [`temporal.py`](../src/camera_platform/temporal.py)의 `_optical_rates`는 rate 배열의
  마지막 축을 3으로 고정합니다. [`hdr_runtime.py`](../src/camera_platform/hdr_runtime.py)는
  이 rate를 시간 적분한 뒤 CFA 위치의 전하를 선택하여 실제 electrical readout에
  전달합니다. 신규 사이트 축은 이 경로 전체에서 보존해야 합니다.
- 기존 pyisetcam의 physical/FDTD 경로에는 4사이트 계산과 단위 시험이 있습니다.
  이것은 플랫폼의 immutable Assembly→Worker→RAW 경로가 새 응답을 사용한다는
  증거가 아닙니다. 스펙트럼 적분도 기존 pyisetcam 경로는 bin-width 합산을,
  플랫폼은 trapezoidal 적분을 사용하므로 두 경로의 수치를 무조건 동일하다고
  비교하지 않습니다.

## 먼저 분리해야 할 FDTD 정규화

[`fdtd_estimate.py`](../src/pyisetcam/fdtd_estimate.py)의
`camerae2e_fdtd_response_v1`은 2×2 **셀 전체 입사광** 대비 사이트별 silicon 흡수
분율 `A_site`를 보존합니다. 유효 사이트 응답은 `4 × A_site × collection_efficiency`
이며, 사이트 간 광학 재분배 때문에 개별 값이 1을 넘을 수 있습니다. 보존 조건은
개별 사이트의 상한 1이 아니라 셀 전체의 reflection·transmission·흡수 합입니다.

예를 들어 `A=[.35,.10,.10,.05]`, collection=.8이면 유효 응답은
`[1.12,.32,.32,.16]`입니다. 균일한 셀 조명에서 전체 수집 전하는 셀 입사 광자의
48%이므로 첫 사이트의 1.12는 에너지 생성이 아닙니다. 이를 1로 자르거나 기존
플랫폼의 독립 픽셀 QE 상한 검사에 그대로 넣으면 의미가 달라집니다.

반대로 이 네 값을 임의의 고대비 영상에서 각 픽셀의 서로 다른 입사광에 곱하는
것도 일반적인 광학 재분배 계산이 아닙니다. 공간적으로 달라지는 입력에 대한
사이트 간 전달 행렬 또는 공간 커널이 필요합니다. 따라서 아래 첫 구현은
**독립 사이트 응답**으로 제한하고 FDTD 셀 분율을 이 계약으로 자동 변환하지 않습니다.
후속 FDTD 연결은 명시적인 셀 내부 균일 조명 제한을 검증하거나 전달 연산자를
추가한 별도 계약이어야 합니다.

감사 시 발견한 `reports/fdtd_sensor_e2e/solver_v3/responses/`의 3개
`*_unverified.json`은 모두 samples=0, status=unverified이며 5개 수치 gate가 false입니다.
현재 이 파일들을 검증된 실제 FDTD 모델로 등록할 수 없습니다. 합성 시험 LUT와
실제 solver 근거를 구분해야 합니다.

## 제안 Sensor 계약

신규 `src/camera_platform/native_pixel_response.py`에 다음 discriminator의 검증과
순수 광전자율 계산을 둡니다. 아래 이름은 제안이며 아직 공개 API가 아닙니다.

```json
{
  "schema": "camera_native_pixel_response_v1",
  "site_names": ["R", "Gr", "Gb", "B"],
  "site_index_tile": [[0, 1], [2, 3]],
  "pixel_pitch_um": 2.0,
  "reference_plane": "sensor_surface_before_pixel_optics",
  "direction_convention": "propagation_into_sensor_x_column_y_row",
  "polarization": "unpolarized",
  "response_basis": "independent_site_collected_electrons_per_incident_pixel_photon",
  "wavelength_nm": [500, 600],
  "theta_deg": [0, 20],
  "phi_deg": [0, 90, 180, 270],
  "interpolation": "linear_wavelength_theta_periodic_phi",
  "values_axis_order": ["wavelength", "theta", "phi", "site"],
  "values": [
    [
      [[0.2,0.4,0.6,0.8], [0.2,0.4,0.6,0.8], [0.2,0.4,0.6,0.8], [0.2,0.4,0.6,0.8]],
      [[0.25,0.45,0.65,0.85], [0.2,0.4,0.6,0.8], [0.15,0.35,0.55,0.75], [0.2,0.4,0.6,0.8]]
    ],
    [
      [[0.3,0.5,0.4,0.1], [0.3,0.5,0.4,0.1], [0.3,0.5,0.4,0.1], [0.3,0.5,0.4,0.1]],
      [[0.35,0.55,0.45,0.15], [0.3,0.5,0.4,0.1], [0.25,0.45,0.35,0.05], [0.3,0.5,0.4,0.1]]
    ]
  ],
  "source": {
    "kind": "estimated",
    "description": "Authored independent-site response; physical validation not established"
  }
}
```

위 수치는 설명용 합성 예시이며 실제 센서의 측정 응답이 아닙니다.

- 사이트는 full native sensor 원점에 고정합니다. 출력 crop의 `(row,col)`은
  `site_index_tile[(row+crop_y)%2][(col+crop_x)%2]`를 사용합니다. crop 시작점으로
  사이트를 재정의하거나 Gr/Gb를 합치지 않습니다.
- +x는 열 증가, +y는 행 증가, +z는 픽셀 광학층으로 들어가는 광자의 진행 방향입니다.
  theta는 +z와의 각도, phi는 +x에서 +y 방향의 방위각입니다. Pupil을 향해 거슬러
  추적한 방향은 부호를 바꿔야 하며 180도 차이를 출처 문구만으로 처리하지 않습니다.
- 파장과 theta는 유한한 증가 축이며 공급 범위 밖 요청을 거부합니다. phi는
  `[0,360)` 주기 축으로 0을 포함하고 중복 360 노드를 받지 않습니다. 정상 입사
  응답은 모든 phi에서 같아야 합니다. normal-only 모델은 비정상 입사를 거부합니다.
  비정상 입사 모델은 전체 주기를 덮는 명시적인 노드를 요구합니다.
- `values`는 정상 입사에 대한 비율이 아닌 **절대 수집 EQE**입니다. 첫 독립 사이트
  계약에서는 유한한 `[0,1]`을 요구합니다. 정상 입사 응답이 0인 사이트도 표현할 수
  있으며 이를 분모로 나누지 않습니다. 셀 정규화·avalanche 응답은 별도 계약입니다.
- CFA, microlens, active silicon 흡수, collection을 포함하는 최종 응답입니다.
  기존 `qe × relative_response`, fill factor 또는 collection을 추가로 곱하지
  않습니다. 전기 readout의 별도 공간 PRNU 가정과 이미 포함된 측정 보정도 중복 여부를
  선언해야 합니다. 원래 3채널 모델은 기존 의미로 유지합니다.
- 기존 `sensor_effective_qe`와 `sensor_angular_response` 효과를 대체하는 새 응답으로
  취급합니다. 포함 효과·기준면·정규화·출처를 고정하고 동일 효과를 두 모델에서
  동시에 활성화하면 거부합니다. physical estimate의 lower-bound 모드는 결과에도
  그 해석을 유지하며 실물 검증 권한을 부여하지 않습니다.

## Lens 방향 분포와 전하 계산 연결

신규 Sensor 응답만으로는 충분하지 않습니다. 기존 theta 분포에서 손실된 phi를
균일하다고 추정하여 복구하지 않습니다. Lens 입력에는 센서 입사면 기준의 결합
광량 분율 `weights[wavelength,field_radius,theta,phi]` 또는 이에 동등한 명시적
방향 벡터·광량 표본이 필요합니다. 각 파장·field의 가중치 합은 1이며 이미 적분된
광량 분율이므로 추가 `sin(theta)` 또는 solid-angle factor를 곱하지 않습니다.

현재 centered 처방과 radial stop mask 범위에서는 기준 field 방향에서 계산한
결합 분포를 native 수광 위치의 방위각만큼 회전할 수 있습니다. 이 회전은 입사
방향 전체에 적용하고, 서로 독립인 theta·phi 주변분포를 곱해서 만들지 않습니다.
Tilt/decenter·비대칭 mask·임의 field의 회전 대칭은 추정하지 않습니다.

[`lens_factory.py`](../src/camera_platform/lens_factory.py)의 현재 on-axis sampling은
회전 대칭을 사용하여 한 방위각의 광선으로 완전한 annulus를 적분합니다. 방위각에
민감한 Sensor와 결합할 때는 해당 annulus의 방위각 적분을 추가해야 합니다.
현재 저장된 on-axis 방향을 하나의 실제 조명 방향으로 해석하면 잘못됩니다.

계산 순서는 다음과 같습니다.

1. 기존 절대 E/L projection과 조건부 PSF, RI로 수광 위치의 분광 조도를 구합니다.
   stop 손실이나 f-number 광량 계수를 재적용하지 않습니다.
2. 각 파장에서 결합 방향 분율로 4사이트의 절대 응답을 평균합니다.
3. 분광 광자 조도와 응답을 파장 적분하고 native pixel 면적을 한 번 곱합니다.
   결과는 `[H,W,4]`의 `electron/pixel/s`이며 readout·exposure·dark를 포함하지 않습니다.
4. 시간 적분 후 full native 좌표로 사이트를 선택하여 `[H,W]` 평균 전하를 만듭니다.
   기존 electrical readout으로 실제 integer RAW를 만들고 ISP/HDR는 그 RAW를 소비합니다.

Pixel-integrated PSF가 이미 수행한 수광 aperture 적분을 다시 하지 않습니다.
PSF와 수광 위치 응답의 분리는 현재의 근사이며 일반적인 공간·각도 결합 전달을
해결했다는 뜻이 아닙니다.

최소 코드 범위는 신규 순수 응답 모듈, Lens 방향 분포 생성·publication,
Sensor 등록 계약, `spatial._spectral_sensor`, `temporal._optical_rates`의 사이트 축,
HDR 연결 및 단위·통합 시험입니다. 전체 `[H,W,Wavelength,Theta,Phi,4]` 배열을 만들지
않고 파장/방향을 분할 적분하며, 모델 노드 수·crop·프레임·메모리 한도를 실행 전에
검사해야 합니다.

## 수용 계산 세 가지

1. **4사이트 전하와 native crop.** 서로 다른 R/Gr/Gb/B 응답의 명시적인 합성 LUT와
   분광 조도를 사용해 `area × exposure × integral(photon_density × EQE)`를 독립
   계산합니다. 잡음·dark를 끄고 포화 전 평균 전하 및 실제 RAW를 ADC 양자화 범위에서
   비교합니다. Gr/Gb 차이가 남아야 하며, odd crop origin을 포함한 네 Bayer 위상의
   crop RAW가 전체 영상의 같은 native 좌표와 일치해야 합니다. 동일 G 응답과
   phi 불변 조건에서는 기존 경로와의 회귀 일치를 별도로 확인합니다.
2. **방위각·광량 결합.** `Q_s=a_s+b_s sin(theta) cos(phi)`에 해당하는 경계 내 합성
   응답과 알려진 두 방향의 광량 분율을 사용합니다. LUT 노드에서의 가중합과 독립
   전하 계산을 비교하여 90°/180° 방향 회전이 예상대로 실제 RAW를 바꾸는지 확인합니다.
   0/360 seam 보간과 on-axis 균일 방위각의 비대칭 항 상쇄를 검증합니다. 같은 방향
   분포에서 입력 절대 조도를 1/4로 만들면 포화 전 전하도 1/4이어야 합니다. 이 검사는
   phi 삭제, 방향 부호 오류, 독립 주변분포 가정, 중복 광량 적용을 검출해야 합니다.
3. **시간 적분과 실제 readout 연결.** 같은 4사이트 광전자율의 정적 장면으로 1배·2배
   노출의 전하와 전기 모델의 알려진 ADC 관계를 독립 계산합니다. 포화 전 전하는
   정확한 시간 비례를 보이고 Gr/Gb 차이는 실제 RAW까지 유지해야 합니다. 사이트·phi
   응답을 바꾼 별도 모델은 새 RAW를 만들고, HDR에는 이 RAW만 전달되어야 합니다.
   포화·잡음이 있는 출력에는 단순 2배 RAW 주장을 하지 않습니다.

최소 실행 증거는 immutable Sensor 모델·Assembly·Test를 고정한 실제 API/Worker
실행입니다. 원본 응답과 방향 분포의 hash, native geometry/site map, 광전자율,
row/frame 시간, 실제 RAW/RGB, parent bundle을 보존합니다. 재시작 후 같은 ID의
기록·artifact를 다시 읽어 byte hash를 확인하고, 응답 blob 손상·축/기준면 불일치·
중복 효과·지원하지 않는 셀 정규화는 제출 시와 Worker 실행 시 모두 거부해야 합니다.
이 증거는 계산 연결과 재현성에 한정되며 실물 픽셀의 정확도를 입증하지 않습니다.

## 남는 위험과 후속 범위

- 4사이트 scalar 응답은 사이트 간 crosstalk 전달 행렬이 아닙니다. FDTD의 2×2
  periodic-cell illumination과 임의 장면의 차이를 남겨 두며 기존 미검증 LUT를
  사용 가능한 실물 모델로 승격하지 않습니다.
- 기존 physical estimate는 green 두 사이트의 실제 차이를 자동으로 만들지 않습니다.
  차별화된 응답에는 명시적인 모델 입력 또는 별도 근거가 필요합니다. Phi 변화의
  합성 수용 시험은 측정된 pixel anisotropy의 증거가 아닙니다.
- 편광·vector diffraction·위상·coherent illumination·일반 pupil Jacobian,
  microlens 배열 간 전달, TCAD의 공간별 collection은 해결 범위 밖입니다.
- coarse angular/field LUT의 보간 오차와 PSF/응답 분리 오차를 보고해야 합니다.
  refinement의 일치가 전체 물리 예측 오차 또는 실물 검증을 뜻하지 않습니다.
- 실제 센서에 적용하려면 방향축·Bayer 원점·pixel pitch·광학층 입사 기준면의
  제조사/측정 좌표 대응과 독립적인 각도별 실측 근거가 필요합니다.

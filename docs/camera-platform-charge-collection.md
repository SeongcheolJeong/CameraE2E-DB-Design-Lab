# Native optical generation and charge collection

이 기능은 **광학적으로 생성된 전하**와 **어느 픽셀에 수집되는 전하**를 별도 모델로
정의하고, 수집 결과를 실제 RAW 및 ISP 입력까지 전달한다. 현재 구현은 선형·즉시 수집
확률 모델이다. 실물 센서 검증, 일반 TCAD 수송 해석, 소자 구조에서의 모델 자동 추출을
완료했다는 의미는 아니다.

2026-09-27 개발 소스는 수신 `capture_crop`에 필요한 **PSF와 전하 이동의 합성 halo**를
검증한 부분 native source도 처리한다. 렌더 작업은 고정된 Sensor의 수집 offset으로
필요한 generation 범위를 계산하고, Lens PSF까지 확장한 분광 source를 출판한다.
공간 실행은 출판된 수신 ROI와 Sensor ID·모델 해시·수집 artifact 해시를 다시 확인한다.
이 경로의 정확성은 공급된 source가 수신 ROI로 들어올 모든 센서 내부 기여를 포함한다는
계산 계약에 한정되며, 장면 렌더러의 물리 정확도나 실물 센서 정확도를 입증하지 않는다.

기존 `camera_native_pixel_response_v1`은 CFA·microlens·흡수·전하 수집을 이미 포함한
collected-electron EQE이다. 이 데이터에 collection 확률을 다시 곱하지 않는다.
새 경로에는 생성 전하를 정의하는 모델과 별도의 수집 모델이 모두 필요하다.

## 모델 경계와 등록

Sensor payload의 기존 `native_pixel_response` 위치에 아래 두 종류 중 하나를 넣는다.

| 광학 응답 schema | `response_basis` | Collection 조합 |
| --- | --- | --- |
| `camera_native_pixel_response_v1` | `independent_site_collected_electrons_per_incident_pixel_photon` | 별도 collection 금지 |
| `camera_native_generation_response_v1` | `independent_site_generated_electrons_per_incident_pixel_photon` | `charge_collection` 필수 |

Generation 응답은 wavelength·theta·phi·R/Gr/Gb/B의 기존 native LUT 축을 사용한다.
CFA·microlens·흡수에 따른 생성 효율을 나타내며, collection을 포함하지 않는다.
현재 각 값은 0..1이며, 단일 전자 생성 범위를 가정한다. Collected EQE만으로 생성
효율과 collection 효율을 식별할 수 없으므로 자동 변환하지 않는다.

Collection은 별도 `camera_charge_collection_v1` JSON이며, 정확한 필드는 다음과 같다.

```json
{
  "schema": "camera_charge_collection_v1",
  "site_names": ["R", "Gr", "Gb", "B"],
  "site_index_tile": [[0, 1], [2, 3]],
  "pixel_pitch_um": 2.0,
  "wavelength_nm": [450, 650],
  "destination_offsets_rc": [[0, 0], [0, 1]],
  "values_axis_order": ["wavelength", "source_site", "destination_offset"],
  "probabilities": [
    [[0.8, 0.1], [0.7, 0.2], [0.75, 0.15], [0.6, 0.25]],
    [[0.6, 0.3], [0.65, 0.25], [0.7, 0.2], [0.75, 0.15]]
  ],
  "interpolation": "linear_wavelength",
  "input_plane": "silicon_generated_charge",
  "output_plane": "collection_well_before_readout",
  "temporal_model": "instantaneous_memoryless",
  "boundary": "absorb_outside_full_sensor",
  "angular_assumption": "direction_independent_depth_marginal",
  "statistics": "single_destination_poisson_thinning",
  "regime": "linear_low_injection",
  "source": {
    "kind": "estimated",
    "description": "Authored illustrative transfer probabilities; not a measured device"
  }
}
```

각 source site 행의 확률 합은 1 이하여야 한다. 부족한 확률은 intrinsic collection
loss이며, 입력을 정규화해서 강제로 합 1로 만들지 않는다. `[dr,dc]`는 source에서
destination으로 향하는 native pixel offset이다. 양의 dr은 아래 행, 양의 dc는 오른쪽
열이다. Source와 offset으로 destination site가 결정되므로 별도 destination CFA 계수를
곱하지 않는다.

Generation과 collection의 wavelength grid, pitch, site 이름, tile은 정확히 같아야 한다.
Collection 파장은 선형 보간하며 외삽하지 않는다. Source 정보, 모델 부모, 적용 조건,
유효 범위와 불확실도는 등록한 Sensor 모델 및 response artifact와 함께 보존한다.
입력 source가 `measured`라고 선언되더라도 결과의 실물 검증 상태가 자동 승격되지는 않는다.

등록 효과에는 기존 `sensor_effective_qe` 대신 `sensor_charge_generation`과
`sensor_charge_collection`을 선언한다. 측정·시험·예측 비교에서도 이 효과 구분을 유지해야
한다. 기존 effective-QE 시험의 효과를 그대로 두고 collection 결과와 비교해서는 안 된다.

## 계산 순서

1. Lens PSF·상대 조도·입사 방향 분포를 적용하여 각 파장의 광학 생성률을 구한다.
2. 해당 위치의 **실제 source CFA site** 생성 효율과 native pixel 면적을 적용한다.
3. 파장별 collection 확률로 기대 생성 전하를 수신 위치에 이동한다.
4. 수신 위치에서 분광 적분하고, 시간 시퀀스에서는 **수신 행의 노출 구간**을 적분한다.
5. 수집 평균에서 Poisson charge를 한 번 샘플링하고, well·read noise·gain·ADC를 적용한다.
6. 저장한 실제 RAW를 ISP와 HDR/DCG merge에 전달한다.

이 순서는 source와 destination의 CFA가 다르거나, 파장마다 이동 방향이 다르거나,
rolling shutter에서 서로 다른 행으로 이동할 때 중요하다. 네 채널 내부 배열은 새 경로에서
실제 destination site 위치에만 값을 갖고, 나머지 세 가상 site 값은 0이다. 독립적인
네 개의 full-color 영상으로 해석해서는 안 된다.

`source_position_ri_v1`을 명시한 경우 Lens 상대 조도는 **소스 위치에서 PSF 전에**
적용하고, 생성 효율과 collection 확률은 PSF 뒤의 실제 수신 native site와 생성 site에
각각 한 번 적용한다. 독립 스칼라 합산 검사는 파장별 소스 RI → 비대칭 PSF →
수신 위치의 각도·방위각별 R/Gr/Gb/B 생성 전하 → 인접 well 이동 → 노출 →
정수 RAW를 한 사례에서 대조한다.
또한 광학·전하 이동의 합성 halo가 있는 부분 source의 수신 ROI를 전체 native 계산과
대조하며, halo가 부족하면 실행 전에 거부한다. 이는 합성 계수의 계산 순서와 보존 계약
검증이며 실제 Pixel의 수집 확률을 측정한 근거는 아니다.

통계 가정은 독립 Poisson 생성 사건과 단일 destination 수집이다. 이 가정에서는
destination 기대값을 먼저 계산한 뒤 Poisson 샘플링하면 각 destination의 평균과 분산이
맞는다. 샘플링한 source charge 또는 RAW를 커널로 convolution하면 다른 분산과 공간
공분산을 만들므로 지원하지 않는다. Shared-charge DCG는 수집 이후 샘플링한 동일 전하를
두 readout 경로에 전달한다.

Collection은 전하 지연이나 이력을 갖지 않는다. Destination row가 수집하는 동안의
순간 생성률을 적분하는 모델이며, 다른 row의 reset 상태가 수송 확률에 미치는 영향은
모델링하지 않는다. HDR/LFM의 기존 광원 파형·노출·fusion 제약도 그대로 적용된다.

## 결과와 보존 근거

Core `transport(generated, wavelength_nm, model)`은 완전한 native sensor grid를 받거나
`origin`과 `full_sensor_shape`를 함께 지정한 부분 source를 받는다. 해당 파장의 기대
생성률 또는 기대 전하에서 입력 단위를 그대로 유지하고 노출이나 잡음을 추가하지 않는다.

| Core 배열 | 의미 | 좌표 |
| --- | --- | --- |
| `generated` | 입력 기대 생성 전하/생성률의 분리된 복사본 | Source pixel |
| `collected` | 수신 위치에 합산된 기대 전하/전하율 | Destination pixel |
| `intrinsic_loss` | 확률 합이 1보다 작은 부분의 손실 | Source pixel |
| `off_sensor_loss` | 목적지가 full sensor 밖인 수집 확률의 손실 | Source pixel |
| `outside_support_collected` | 부분 입력의 바깥이지만 실제 센서 안으로 이동한 전하; 부분 source에서만 기록 | Source pixel |

전체 입력에서는 `generated = collected + intrinsic_loss + off_sensor_loss`이고,
부분 입력에서는 여기에 `outside_support_collected`가 더해진다.
각 배열의 합과 부동소수점 잔차를 기록한다. Native sensor 밖으로 나가는 전하는 흡수
경계에서 사라지며, 반사하거나 남은 목적지로 재분배하지 않는다. 부분 source의 바깥으로
나간 센서 내부 전하는 물리적 손실로 계상하지 않는다. 부분 source의 전체 수집 지도는
공급하지 않은 바깥 source의 유입이 빠져 있을 수 있지만, 검증된 수신 ROI는 완전한 유입
halo를 갖는다.

정적 공간 실행은 `collection_generated`, `collection_intrinsic_loss`,
`collection_off_sensor_loss`를 전자/pixel 단위로 저장한다. 수집 결과는
`mean_electrons`에 있다. `summary.charge_collection`에는 전체 전자 수와 보존 잔차가 있다.

시간 실행은 각 고유 장면의 `collection_*_rate_scenes`를 전자/pixel/s 단위로 보존한다.
장면별 보존 진단은 `flux_diagnostics_per_scene[*].charge_collection`에 있다. 이것은
노출 전 장면의 전자율이며 프레임에서 수집한 전하와 혼동하면 안 된다. 수신 행의 노출이
서로 다를 때 source 행별 노출로 loss를 적분하여 프레임 보존식으로 표시하지 않는다.

`native_generation_efficiency`, `collection_probabilities`,
`collection_destination_offsets_rc`와 모델 response artifact를 함께 보존하여 계산 입력을
확인할 수 있다. RAW·ISP 출력과 calibration은 기존 경로를 사용하며, merge가 simulator의
generated/collected truth 배열을 관측값처럼 사용하지 않는다.

## 지원 범위와 아직 남은 작업

- 공간·기록된 시간 시퀀스·전기 HDR·shared-charge DCG에서 native 생성/수집 경로를 사용한다.
  Reference HDR는 같은 전기 capture 경로를 사용하되 기존 reference fusion 제약을 유지한다.
- 영상 runtime의 부분 입력은 명시적 `capture_crop`과 Lens PSF·수집 offset의 합성 halo가
  있어야 한다. 조건을 충족하지 않는 source는 거부한다. Core 입력 픽셀 상한은
  1,048,576이고, 영상 runtime source의 각 변은 짝수 8..512, 파장은 2..16개다.
  일반 임의 타일링이나 전체 대형 센서의 무제한 실행은 아직 제공하지 않는다.
- Collection LUT는 2..256개 wavelength node, 4 source site, 1..81개 고유 offset을
  지원한다. 각 offset 성분은 -4..4 pixel이다. 값·축·bool·중복·범위를 엄격하게 검사한다.
- Routing workload는 `pixel 수 × 파장 수 × offset 수`로 예산을 계산한다. 단일 장면은
  128,000,000, 시간 시퀀스 전체 고유 장면 합은 512,000,000 routing sample 이하여야 한다.
  기존 방향 적분·PSF·영상 메모리 예산도 함께 적용된다.
- Core의 source 값 상한은 pixel당 `1e13`이다. Runtime이 core에 전달하는 값은
  **분광 생성률 electron/pixel/s/nm**이므로, 이 상한은 노출 시간으로 줄어드는 적분 전하
  상한과 별개이다. 높은 순간 생성률을 아주 짧은 노출로 보상하는 입력도 거부될 수 있다.
- 새 경로의 PRNU는 0이어야 한다. 생성 위치에 적용할 물리적 PRNU 모델과 수집 후
  receiver response 모델을 구별하여 구현하기 전에는 수집 전하를 임의로 다시 배율 조정하지 않는다.
- Quick Estimate에는 공간 수집 격자가 없으므로 이 pair를 거부한다. Native closed-loop
  AE/AWB, collection 모델 fitting·자동 최적화, 일반 3D 수송, 지연·trapping·blooming,
  high-injection 상호작용, analog charge splitting은 구현 범위 밖이다.
- `direction_independent_depth_marginal`은 파장별 생성 깊이 분포를 축약하고 그 결과가
  입사 방향에 독립적이라고 가정한다. 일반 각도·편광·깊이별 TCAD weighting function이나
  FDTD voxel generation map을 자동으로 축약한 모델은 아니다.
- 2×2 native site 주기와 고정 확률을 사용한다. 센서 위치·온도·bias별 변화는 LUT 축으로
  추가되지 않았으며, 입력 모델의 적용 조건에 맞는 범위에서만 사용할 수 있다.

테스트는 source CFA와 destination CFA가 다른 비대칭 이동, 파장별 반대 이동, 손실·보존,
receiver rolling window, 실제 RAW/RGB 변화, 동일 DCG charge, 모델·artifact 변조 거부와
기존 경로의 결과 보존을 확인한다. 이러한 계산 검증은 독립 실물 센서 검증을 대체하지 않는다.
최종 실행 횟수와 검증 상태는 해당 개발 인계 및 evidence report를 기준으로 한다.

## 물리 경계의 참고 자료

Ansys는 광학 생성률과 공간적 수집 weighting function을 분리해 EQE를 계산한다.
이 구현은 해당 분리를 유지하면서 수송을 제한된 확률 LUT로 표현한다.
[CMOS Sensor Camera — Sensor Characterization](https://optics.ansys.com/hc/en-us/articles/360062131614-CMOS-Sensor-Camera-Sensor-Characterization)

광학적 crosstalk와 생성 전하의 전기적 이동은 서로 다른 현상이며, EQE에는 광학·전기적
손실이 함께 포함된다.
[CMOS — Electrical simulation methodology](https://optics.ansys.com/hc/en-us/articles/360042358674-CMOS-Electrical-simulation-methodology)


## 개발 검증 재현

`tools/verify_camera_charge_collection.py`는 기본 설치본 8770을 거부하고 별도 QA
서버에 공개 API/SDK로만 입력을 등록한다. 광학 Factory의 실제 처방 결과에 독립적으로
작성한 합성 generation/collection 모델을 조합한다. 이 작성값은 실제 센서 측정값이 아니다.

```sh
.venv-platform/bin/python -m camera_platform serve --port 51263 \
  --state-dir outputs/camera-platform-charge-collection-v016-qa
.venv-platform/bin/python tools/verify_camera_charge_collection.py \
  --base-url http://127.0.0.1:51263 \
  --state-dir outputs/camera-platform-charge-collection-v016-qa \
  --output-dir reports/camera_platform/charge-collection-v016/http
```

기존 증거 경로는 입력·소스·환경이 같은 경우에만 이어갈 수 있다. 처음부터 다시
실행하려면 새 QA state와 새 output 경로를 사용한다. 재시작 후에는 같은 인자에
`--verify-only`를 추가하여 기존 전체 기록·artifact·bundle manifest를 다시 확인한다.
`plot_camera_charge_collection.py`는 저장된 bundle의 SHA를 검증한 뒤 도표를 그린다.

Workbench의 모델 JSON 등록에서 두 계약을 함께 등록하고 고정 카메라에 연결한다.
결과 화면은 생성·수집·수집 손실·센서 밖 손실을 별도로 표시한다. 시간 촬영에서 표의
값은 장면별 **노출 전 전자율**이며 프레임의 적분 전하 합이 아니다. 모델 복사 초안도
두 계약을 함께 보존한다. 기존 collected EQE를 generation으로 자동 변경하지 않는다.

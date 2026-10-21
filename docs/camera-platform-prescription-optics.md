# 처방 기반 OPD·Field PSF와 Camera Runtime

**v0.14.0을 기본 작업실에 설치했습니다.** [설치 전환](../reports/camera_platform/primary-prescription-upgrade.json)은 백업과 기존 215개 기록·74개 작업·437개 감사 이력 보존을 확인합니다. [실제 기본 작업실 계산](../reports/camera_platform/prescription-primary-delivery.json)은 동일 처방의 광량·위치별 PSF, 새 Lens/Camera, 64×64 RAW/RGB와 RAW를 보존한 ISP 재처리를 연결했습니다. [기본 화면 조회](../reports/camera_platform/prescription-ui/primary-v014/verification.json)와 [기존 시간 RAW/RGB 재현](../reports/camera_platform/primary-prescription-legacy-rerun.json)도 확인했습니다.

[후보 감사](../reports/camera_platform/prescription-v014-candidate-audit.json)의 중복 제외 1,450개 시험, 패키지·격리·재시작 검증에 더해 설치 QA에서 처방 10·준비 20·장면 작성 36·광량 재사용 26개 UI 항목을 확인했습니다. 처방 UI의 장면 준비 검사는 PSF 범위 표시와 다른 조명 조건의 저장 거부까지이며, 처방 카메라의 일반 3D 결상을 검증했다는 뜻은 아닙니다. [최신 인도 기록](../reports/camera_platform/delivery.json)과 [요구사항 감사](camera-platform-requirements-audit.md)를 따릅니다.

`prescription_psf_v1`은 구면·평면 렌즈 처방에서 RayOptics로 파장·시야별 광로차(OPD)와 통과 동공을 계산하고, Prysm으로 픽셀 적분 PSF를 만듭니다. 같은 처방에서 생성한 RI·입사각 응답에 이 PSF를 연결하여 Native 분광 이상 영상에서 RAW/RGB를 계산하는 경로입니다. 독립적으로 선언한 수차 계수를 사용하는 기존 `prysm_psf_v1`과 생성 근거를 구분합니다.

**독립 실물 검증과 총 예측 불확실도는 미확립입니다.** 계산이 수락되어도 `source_kind=simulated`, `approval_status=unapproved`, `physical_validation=not_established`, `uncertainty={"kind":"unknown"}`을 유지합니다. 수치 일치와 근사 적용 영역 검사는 실물 정확도나 예측 오차의 상한을 뜻하지 않습니다.

## 계산과 등록 흐름

```text
동일한 lens_prescription_v1 원본
  ├─ rayoptics_lens_v1 → 분광 RI·입사각 분포 → 부모 Lens 모델
  └─ prescription_psf_v1 → 처방 광선·OPD·통과 동공
                          → 근축 균일 진폭 동공의 회절
                          → 파장·시야별 픽셀 적분 PSF 후보
부모 Lens + PSF 후보 + 고정 적용 조건
  → 별도 자식 Lens 모델 → Camera 구성
  → Native 분광 이상 영상 → source-position PSF
  → receiver-position RI·각도 평균 QE → RAW → 명시적 ISP → RGB
```

처방 계산은 [prescription_psf.py](../src/camera_platform/prescription_psf.py), 후보 등록과 부모 검증은 [factory.py](../src/camera_platform/factory.py), 영상 연산은 [spatial.py](../src/camera_platform/spatial.py)에 있습니다. 이 경로의 PSF는 MTF 크기만으로 역산한 값이 아닙니다.

## 지원하는 처방과 적용 조건

내부 `prescription`은 기존 [lens_factory.py](../src/camera_platform/lens_factory.py)의 `lens_prescription_v1` 검사를 그대로 받습니다. 중심이 맞은 회전 대칭 구면·평면, 명시적 개구와 하나의 비굴절 평면 Stop, 파장마다 직접 제공한 실수 굴절률, 물체측·상측 공기를 지원합니다. 코팅·흡수 손실을 포함하지 않는 `ideal_lossless` 계약입니다. 처방의 마지막 두께가 정하는 센서 평면을 유지하며 자동 초점 조정은 하지 않습니다.

물체는 `infinity_plane_wave`로 고정합니다. RayOptics 모델에서는 매우 먼 물체 평면으로 무한 공액을 표현합니다. 유한 물체 거리, 깊이별 재초점, 비구면·Tilt·Decenter·비대칭 광학계는 이 Adapter의 지원 범위에 포함되지 않습니다.

| 요청 항목 | 계약 |
| --- | --- |
| `schema` | `prescription_field_psf_v1` |
| `reference_focal_length_mm` | 모든 파장이 공유하는 이상적인 직선 투영의 기준 초점거리, 0.5–500 mm |
| `field_y_mm`, `field_x_mm` | 센서 중심 기준의 엄격히 증가하는 좌표; 각 축 2–9개, 0을 포함하는 범위, 전체 최대 25개 시야 노드 |
| `pixel_pitch_um` | 수신 픽셀 Pitch, 0.2–20 µm |
| `pupil_samples` | 초기 동공 격자의 한 변, 짝수 16–128; 별도로 두 배 격자 계산 |
| `kernel_size` | PSF의 한 변, 홀수 3–65 픽셀 |
| `subpixel_samples` | 픽셀당 초기 수신 표본 수, 홀수 3–9; 별도로 `2s+1` 표본 계산 |

처방의 파장은 350–1100 nm 안의 2–16개 정확한 좌표입니다. 파장마다 별도의 단색 모델을 만들어 명시한 굴절률을 사용하며, 파장 보간이나 라이브러리의 재료 추정을 추가하지 않습니다. Backend는 RayOptics 0.9.8과 Prysm 0.21.1로 제한합니다. 요청 템플릿도 수락 결과를 보장하지 않으며 선택한 시야·개구·격자로 아래 검사를 통과해야 후보가 생성됩니다.

## OPD·좌표·단위

각 시야의 반경 `r=hypot(x,y)`와 선언한 기준 초점거리로 물체측 각도 `atan2(r, reference_focal_length_mm)`를 정합니다. 회전 대칭 처방을 RayOptics의 자오면에서 추적한 뒤 동공과 광선 좌표를 함께 회전하여 X/Y 시야를 구성합니다. Chief ray가 작성한 Stop 중심에 도달하는지와 물리 개구가 Backend에 의해 바뀌지 않았는지를 검사합니다. 막힌 광선·표면 미도달·전반사는 실패 종류별로 기록하고 통과 동공에서 제외합니다.

기준 영상점은 해당 시야의 공통 이상 좌표입니다. 파장별 Chief 위치로 PSF를 다시 가운데로 옮기지 않으므로, 이 기준과 실제 광선 위치 사이의 변위가 유한 커널 안에 남을 수 있습니다. 이는 **일반 왜곡 맵·임의 영상 재투영 기능을 제공한다는 의미는 아닙니다.**

| 값 | 단위와 의미 |
| --- | --- |
| 처방의 곡률 반경·두께·개구, 광선 좌표 | mm |
| `opd_nm` | Chief 광로에서 해당 광선의 광로를 뺀 OPD. mm 결과에 `1e6`을 곱해 nm로 보존 |
| 동공 위상 | `exp(-2*pi*i*opd_nm/wavelength_nm)`; 부호를 바꾸면 영상 변위의 방향도 바뀜 |
| Prysm 입력 | 동공 간격·전파 거리 mm, 파장·수신 간격 µm. 파장 nm에 `1e-3`을 곱해 전달 |
| `field_y_mm`, `field_x_mm` | 이상 영상의 Source 위치, 전체 센서 중심 기준 mm |
| `psf_kernel` | 수신 픽셀 안의 적분 에너지 비율, 각 파장·시야 커널 합이 1인 무차원 값 |
| `psf_bin_energy`, `retained_energy_fraction` | 유한 커널을 합 1로 재정규화하기 전의 수치 에너지와 보존 비율 |

RayOptics의 [`wave_abr_full_calc`](https://ray-optics.readthedocs.io/en/stable/api/rayoptics.raytr.waveabr.html#rayoptics.raytr.waveabr.wave_abr_full_calc)는 광선·Chief·기준구를 받아 OPD를 계산합니다. 위 부호·mm→nm 변환과 공통 영상점 선택은 이 Adapter의 계약이며 독립 광로 계산 시험과 함께 관리합니다.

동공은 통과 위치에서 진폭 1, 나머지 위치에서 0으로 놓습니다. [`focus_fixed_sampling`](https://prysm.readthedocs.io/en/stable/api/propagation.html#prysm.propagation.focus_fixed_sampling)으로 수신 격자에 복소장을 계산한 뒤 절댓값 제곱을 픽셀별로 합합니다. 수신 픽셀 개구는 여기서 한 번 포함하므로 Runtime에서 같은 Box filter를 다시 적용하지 않습니다.

`reference_focal_length_mm`는 작성자가 고정한 투영 기준입니다. 계산된 파장별 유효 초점거리(EFL)와 입사 동공 지름은 별도 Summary에 보존합니다. 등록 시의 기준 F-number는 `reference_focal_length / entrance_pupil_diameter_at_reference_wavelength`이며, 기준 파장은 파장 배열의 `len(waves)//2` 위치입니다. 이 기준 F-number와 각 시야의 회절 계산에 사용하는 `reference_sphere_radius / exit_pupil_diameter`를 혼동하지 않습니다.

## 수치·근사 적용 영역 검사

초기 동공/수신 표본을 `(n,s)`라 하면 `(n,s)`, `(2n,s)`, `(n,2s+1)`, `(2n,2s+1)`을 비교합니다. 마지막 조합의 정규화 커널을 후보로 사용합니다.

| 검사 | 후보 생성 조건 |
| --- | --- |
| 격자 변경에 따른 커널 차이 | 앞의 세 커널과 최종 커널의 L1 차이가 선언한 `max_kernel_l1_change` 이하; 선언값 최대 0.03 |
| 유한 커널 에너지 | 모든 설정에서 선언한 최소 보존 비율 이상; 선언값 최소 0.95, 계산된 최대 비율 1.001 이하 |
| 인접 동공 OPD | 통과하는 인접 표본의 OPD 차이가 해당 파장의 0.25배 이하 |
| 근축 출사 동공 사상 | 실제 추적 출사 위치와 근축 위치 차이/출사 동공 반경이 선언한 한계 이하; 선언값 최대 0.05 |
| 광선 Cone·기준축 | Cone은 선언한 각도 이하(선언값 최대 10°), 기준축 기울기는 2° 이하 |
| 동공 격자 경계 | 통과 동공이 계산 격자의 테두리에서 잘리지 않음 |
| 수신 표본과 주기 복제 | 회절 Nyquist 간격과 유한 동공 격자의 주기 복제 간격을 만족 |

수치 검사와 근사 적용 영역 검사를 모두 통과하면 `PROPOSED`와 후보를 반환합니다. 검사에 미달하면 `REFUSED`, `model_candidate=null`로 결과와 진단을 남깁니다. 잘못된 요청·추적 오류·시간 한도 초과는 실행 실패가 될 수 있습니다. 샘플 수·DFT 계산량·추적 시간에도 별도 한도가 있습니다. 이 한도는 계산 자원을 제한하며 성능 SLA나 물리 오차를 보증하지 않습니다.

## Native 영상에서 Field PSF를 적용하는 방법

후보는 `field_intensity_psf_v1`입니다. 배열 축 순서는 다음과 같습니다.

```text
values[wavelength, field_y, field_x, kernel_y, kernel_x]
field_coordinates = sensor_center_yx
interpolation = bilinear_source_position
sampling_model = pixel_integrated_optical_impulse
```

Native Source 픽셀 중심의 좌표는 각 Y/X 축에 대해 다음 식으로 계산합니다.

```text
position_mm = (index + crop_origin - (full_sensor_shape - 1) / 2)
              * pixel_pitch_um / 1000
```

Y는 배열 행이 증가하는 방향, X는 열이 증가하는 방향입니다. Crop 자체의 중심으로 좌표를 다시 설정하지 않습니다. 시야 격자는 Crop의 모든 Source 픽셀 중심을 덮어야 합니다. Runtime은 범위 밖 보간, 파장 재표본화, Pitch 변경을 거부합니다. 커널은 홀수 크기이며 배열 중심이 변위 0입니다. `array_center`는 커널의 무게중심을 강제로 중앙에 맞춘다는 뜻이 아닙니다.

각 Source 픽셀의 광량을 주변 시야 노드의 이중 선형 가중치로 먼저 나눈 후 노드별 커널로 Convolution하여 더합니다. **Receiver 위치에서 여러 Convolution 결과를 섞는 연산과 다릅니다.** 전체 Convolution에서 Source의 파장별 광속 보존을 검사하고, Crop 밖으로 이동한 광속과 작은 FFT 음수 반올림 보정량을 별도로 기록합니다. 제공된 영상 바깥은 어둡다고 가정하며 외부 장면의 유입은 계산하지 않습니다.

Runtime 입력은 `W/m^2/nm` 단위의 `sensor_ideal_image_before_psf_ri` 영상입니다. 축상 조리개·투과율 효과는 입력 광량에 이미 반영되어야 합니다. Field PSF 적용 뒤 Receiver 위치의 RI와 입사각 평균 QE를 곱하고, 파장 적분·픽셀 면적·노출·Readout을 거쳐 RAW를 생성합니다. 같은 공간 연산은 시간 촬영의 광전자율 계산에서도 사용됩니다. ISP 재처리는 보존 RAW에서 별도 처리 결과를 만들며 렌즈나 센서의 재촬영을 대신하지 않습니다.

## 부모 계보와 물리 상고 보존

[factory.py](../src/camera_platform/factory.py)는 다음 조건으로 PSF를 기존 Lens 응답에 연결합니다.

- 성공한 PSF 작업과 후보가 존재하고, 부모 Lens와 같은 Lens 자산을 가리켜야 합니다.
- 부모는 원본 연결이 확인된 `rayoptics_lens_v1` 작업에서 나와야 합니다. 부모 작업의 요청 전체가 PSF 요청 안의 `prescription`과 정확히 같아야 합니다.
- 전체 파장 좌표와 적용 조건을 보존합니다. 기준 F-number가 다르거나 부모의 명시된 초점거리와 공통 영상 기준이 다르면 등록을 거부합니다.
- 부모를 덮어쓰지 않고 `parents=[base_lens_id]`, `psf_factory_job_id`, 적용 조건, 두 계산의 근거를 가진 새 Lens 버전을 만듭니다. 등록 후에도 이 정보로 예상 모델을 재구성해 원본 연결을 검사합니다.
- `optical_response` 조건 범위에서는 알려진 후단 제어인 노출·Gain만 새 광학 고정 조건에서 제외합니다. 부모의 기존 유효 범위와 고정 조건은 계속 적용되며, 다른 조건을 임의로 해제하지 않습니다.

부모의 정규화된 RI·입사각 축은 특정 센서 크기가 아니라 **처방에서 계산한 물리 상고**에 대응합니다. 자식에는 `field_reference_radius_mm=prescription.image_height_mm[-1]`을 보존합니다. Runtime은 Native Receiver의 mm 반경을 이 값으로 나누어 응답을 조회하고, 계산한 물리 반경 밖에서는 실행을 거부합니다. 다른 센서나 Crop으로 재사용할 때 RI 곡선을 새 센서 모서리까지 늘리지 않습니다.

계보에는 요청과 환경, OPD, 통과 동공, 광선의 센서/출사 동공 위치, Chief/기준 영상점, PSF와 유한 에너지, 시야·파장 축이 포함됩니다. Camera Run 근거는 자식 Lens와 부모 RI/입사각 작업까지 이어집니다. 계보 보존 자체가 데이터 배포권이나 모델 승인을 제공하지는 않습니다.

Factory 등록 화면은 기준 구성의 조건과 처방에서 계산한 F-number를 합친 정확한 JSON을 먼저 표시합니다. 등록 후 Builder로 이동하면 이 적용 조건을 전달합니다. Builder의 촬영 조건·분광 범위·RAW 계약 JSON을 선택한 Sensor와 Lens에 맞춰 확인·수정한 뒤 새 구성을 저장합니다. 부모 모델이 다른 조리개 등으로 고정되어 있으면 화면의 값 변경만으로 그 유효 범위를 넓힐 수 없으며 서버가 등록을 거부합니다.

## 해석해야 할 한계

**균일 동공 진폭·근축 근사.** 처방의 실제 광선으로 OPD와 개구 통과 여부는 구하지만 출사 동공의 진폭 수송, 사상 Jacobian, Fresnel/코팅 투과, 편광과 벡터 회절은 계산하지 않습니다. 근축 출사 동공에 균일한 통과 진폭을 놓고 스칼라 Fraunhofer 회절을 적용합니다. 기준축에 수직인 영상면으로 근사하며 기울기·Cone·사상 차이 검사는 이 제한을 벗어나는 입력을 거르는 장치입니다. 통과 입력의 정확도를 정량화한 오차 경계는 아닙니다.

**Source PSF와 Receiver RI의 방사 측정 근사.** PSF는 Source 위치의 조건부·합 1 분포이고 RI와 각도 QE는 Receiver 위치에서 별도로 적용됩니다. 같은 처방을 사용하더라도 동공 위치·영상 변위·입사각·편광·에너지의 결합 분포를 푼 것은 아닙니다. Source와 Receiver 사이의 위치·각도·면적 변환을 함께 반영하는 전체 방사 전달 Jacobian도 해결하지 않았습니다. 특히 시야별 왜곡/변위·Vignetting·각도 응답이 함께 변하는 경우, 이 분리 연산과 실제 방사 전달 사이의 오차는 미확립입니다. RI 이전의 이산 광속 보존 검사를 전체 렌즈의 방사 측정 정확도 검증으로 해석하지 않습니다.

**유한 커널과 보간.** 잘린 꼬리의 에너지 비율을 기록하지만 채택된 커널은 합 1로 다시 정규화합니다. 꼬리를 물리적으로 복원하지 않습니다. 시야 사이에서는 강도 커널을 이중 선형 보간하며 중간 시야를 독립 광학 계산으로 검증한 것은 아닙니다. Source Cell의 광량을 중심점에 놓는 이산 적분과 픽셀 수신 적분은 서로 다르며, 연속 Source Cell 적분·Aliasing 복원은 미확립입니다.

**전체 카메라 범위.** 유한 거리·깊이·가림에 따른 PSF, 일반 왜곡 재투영, Stray light, Pixel Crosstalk·전하 수집, 일반 센서 전기 모드·HDR/LFM·임의 ISP/HW Backend와는 별개입니다. 이 개발분만으로 상세 카메라 모델이나 상용화 목표를 완료했다고 판정하지 않습니다.

## 관련 시험과 인도 경계

아래는 개발 구현의 검증 의도를 확인할 수 있는 시험 파일입니다. 이 문서는 실행 횟수·통과 수나 새 설치본 인도를 주장하지 않습니다.

실제 실행 결과와 소스 결합은 [개발 회귀 기록](../reports/camera_platform/prescription-development-test-binding.json), [HTTP·Worker 기록](../reports/camera_platform/prescription-http-verification.json), [브라우저 등록·Builder 기록](../reports/camera_platform/prescription-ui-verification.json)에 별도로 보존합니다. 회귀 실행 후 바뀐 `app.js`의 화면 근거는 브라우저 기록에 고정한 SHA-256을 따릅니다.

| 시험 파일 | 확인하는 계약 |
| --- | --- |
| [test_prescription_psf.py](../tests/platform/test_prescription_psf.py) | 직접 광로와 OPD, 위상 부호·시야 회전, 독립 Airy 픽셀 적분, 처방/파장 변화, 물리 개구와 수락 거부 |
| [test_field_psf_runtime.py](../tests/platform/test_field_psf_runtime.py) | 독립 Source 산포 연산, Convolution 방향·광속·Crop 좌표, 정적/시간 계산, 기존 PSF 경로, 물리 상고와 범위 거부 |
| [test_prescription_workflow.py](../tests/platform/test_prescription_workflow.py) | 같은 처방의 두 Factory→자식 Lens→RAW/RGB·Replay·원본 근거, 다른 처방/조건·위조 계보 거부 |
| [test_field_psf_workflow.py](../tests/platform/test_field_psf_workflow.py) | HTTP 모델 등록·RAW/Replay 근거, 장면 준비·실제 Crop·저장 광휘 재사용의 Field 범위, 폐루프 계산 |

기본 설치 v0.13의 기존 실행과 기록은 이 개발 문서로 변경되지 않습니다. 새 기능을 설치 제품의 지원 목록에 올리려면 별도의 패키지·설치 전환·실제 실행·기존 기록 보존 근거를 남겨야 하며, 이후에도 독립 실측과 총 불확실도가 확보되기 전까지 물리 검증 상태는 미확립으로 유지합니다.

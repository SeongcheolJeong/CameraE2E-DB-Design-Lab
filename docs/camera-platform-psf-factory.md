# 선언한 동공의 PSF Factory

`camera_platform.psf_factory`는 명시적으로 선언한 원형 동공의 진폭·광로차를 실제 Prysm 0.21.1로 전파하여 파장별 픽셀 적분 PSF를 계산한다. 네 가지 수치 설정을 비교하고 유한 영상 범위에 남은 에너지를 기록한다. 검사 통과 시 `intensity_psf_v1` 후보를 반환하며, 미통과 시 결과·진단을 보존하고 후보는 반환하지 않는다.

**이 동공은 RayOptics Lens의 실제 Wavefront를 추출한 것이 아니다.** 원형·균일 진폭의 독립 광학 가정이다. 기존 Lens의 RI·입사각 응답과 조합해도 그 Lens의 수차나 물리 성능을 검증한 것으로 승격되지 않는다. 공간적 PSF와 입사각 응답을 따로 적용하는 계산 역시 상관을 생략한 근사다. 입력·부모 모델의 적용 조건과 출처는 계속 일치해야 한다.

## 계산 범위와 단위

Prysm의 `focus_fixed_sampling`은 동공 격자 간격과 전파 거리를 mm, 파장과 영상 격자 간격을 µm로 받는다. Adapter는 명시적 nm 입력을 해당 단위로 변환하고, 고정 수신 격자에서 Matrix DFT를 사용한다. [Prysm 0.21.1 propagation API](https://prysm.readthedocs.io/en/stable/api/propagation.html)

| 입력 | 의미 |
| --- | --- |
| `schema` | `declared_pupil_psf_v1` |
| `wavelength_nm` | 350–1100 nm 안의 정확히 증가하는 2–16개 파장. 묵시적 보간은 하지 않는다. |
| `pupil_diameter_mm` | 0.1–100 mm의 균일 원형 동공 지름 D |
| `f_number` | 2–32. 전파의 초점거리 척도는 `D * f_number`이다. 실제 Lens의 출사동공·유효 F-number를 역추정하지 않는다. |
| `pixel_pitch_um` | 0.2–20 µm. PSF는 이 간격의 정사각형 수신 픽셀에 적분된다. Sensor의 정확한 Pitch와 맞아야 한다. |
| `opd_nm` | `defocus`, `astigmatism`, `coma_x` 세 계수. 각각 ±500 nm 이내이며 RMS 수차 계수가 아니다. |
| `pupil_samples` | 짝수 32–128. 정밀 비교 설정은 이 값의 두 배를 사용한다. |
| `kernel_size` | 홀수 3–65. 광축을 중심으로 한 정사각형 유한 영상 범위다. |
| `subpixel_samples` | 홀수 3–9. 한 Pixel 변의 수신 적분 표본 수이며, 별도 비교 설정은 `2*s+1`이다. |
| `max_kernel_l1_change` | 1e-6–0.03. 수치 설정 변경에 따른 정규화 Kernel의 L1 차이 허용치다. 절대 오차·물리 불확실도가 아니다. |
| `minimum_retained_energy` | 0.95–0.9999. 모든 수치 설정·파장에서 유한 영상 범위에 남아야 할 에너지 비율이다. |
| `application_basis` | `independently_declared_pupil_not_rayoptics_wavefront`를 명시한다. |

현재 기본값은 `request_template()`으로 조회한다. 기본값 변경은 새 요청·환경의 검증 대상이며 기존 결과에 소급하지 않는다.

```python
from camera_platform.psf_factory import characterize, request_template

request = request_template()
# Independent, synthetic zero-OPD verification case; not a measured Lens.
request.update(
    wavelength_nm=[500, 600],
    pixel_pitch_um=2.0,
    pupil_samples=128,
    kernel_size=31,
    subpixel_samples=3,
    opd_nm={"defocus": 0.0, "astigmatism": 0.0, "coma_x": 0.0},
)
result = characterize(request)
candidate = result["model_candidate"]  # None when numerical checks refuse it.
```

동공 반지름을 1로 둔 좌표 x,y와 `r²=x²+y²`에 대해 광로차는 다음과 같다.

```text
OPD_nm = defocus*(2*r²-1)
       + astigmatism*(x²-y²)
       + coma_x*(3*r²-2)*x
pupil  = circular_aperture * exp(2πi * OPD_nm / wavelength_nm)
```

진폭 투과·편광·입사각·비균일 Apodization·중앙 차폐·Pupil magnification·구조 산란은 입력 모델에 없다. 계산은 공기 중 Paraxial scalar Fraunhofer 모델이며, 파장별 복소 진폭의 절댓값 제곱을 비간섭성 Intensity PSF로 사용한다. FDTD나 벡터 회절, 일반 물체 거리·왜곡·시야별 수차 모델을 대체하지 않는다.

## 에너지·픽셀 적분과 수렴 기준

동공 격자는 지름의 1.2배 폭을 사용한다. Prysm의 MDFT 수신 표본 에너지를 입력 동공의 표본 에너지 합으로 나누고, 각 Pixel 내부의 균일 Subpixel 표본 에너지를 합산한다. 결과는 단위 없는 **입력 대비 수신 Pixel 에너지 비율**이다. 표본 중심에서의 PSF 밀도만 평가한 값과 구분한다.

같은 요청에 대해 `(n,s)`, `(2n,s)`, `(n,2s+1)`, `(2n,2s+1)` 네 조합을 실행한다. 동공과 수신 격자의 정밀도 변경을 별도로 확인한다. 각 설정의 유한 범위 에너지 합은 `retained_energy_fraction`에 보존하고, 정규화 Kernel은 가장 정밀한 마지막 설정에서 선택한다.

후보 생성에는 다음이 모두 필요하다.

- 유한·양의 수신 에너지와 정상 Solver 버전
- 모든 설정·파장의 잔존 에너지가 선언한 최소값 이상, 1.001 이하
- 기준 설정 대비 나머지 세 설정의 최대 Kernel L1 차이가 선언한 허용치 이하
- 회절 Intensity의 Nyquist 수신 간격, 이산 동공의 영상 주기 복제와 충분한 분리, DFT 작업량 한도 충족

1.001은 현재 수치 에너지 검사 허용치이며 이 범위의 초과 에너지를 물리 이득으로 해석하지 않는다. 통과한 경우에도 수치 불확실도는 `unknown`이다. 두 수준의 차이는 참값 오차 상한이 아니며, 더 조밀한 격자·독립 해석해·다른 Solver 검증이 필요할 수 있다. 실행 자원 한도는 요청 Preflight에서 검사되며, 서비스의 Worker Timeout·취소 정책은 별도로 적용한다.

유한 PSF 바깥의 광학 꼬리는 반환 Kernel에 존재하지 않는다. 후보는 **남은 에너지의 합이 1이 되도록 재정규화**한다. 따라서 이 Kernel을 절대 Lens 투과율이나 전체 광학 에너지로 해석하면 안 된다. 원래의 Bin 에너지와 잔존 비율은 원본 Artifact로 보존하며, 수집 범위 밖의 광학 꼬리와 Scene 이미지 경계 밖으로 흘러나간 에너지는 서로 다른 손실이다. 범위가 작아 에너지 기준을 충족하지 못하면 정규화만으로 통과시키지 않는다.

## Candidate와 복소 OTF

후보의 `sampling_model=pixel_integrated_optical_impulse`는 수신 Pixel aperture가 이미 한 번 적분되었음을 뜻한다. 소비 Runtime은 여기에 두 번째 수신 Box filter를 적용하면 안 된다. 이산 Scene 입력 Cell을 중심점에 놓는 소스 적분 근사는 별개의 가정이며, 이 Factory가 일반 연속 장면의 Cell 적분까지 해결한 것은 아니다.

후보는 파장별 `[wavelength,y,x]` Intensity Kernel, 정확한 `sample_pitch_um`, `normalization=unit_sum`, `centering=array_center`, `spatial_model=locally_shift_invariant`, `boundary=zero_outside_supplied_image`를 포함한다. 중심은 좌표 원점이며 Coma처럼 비대칭 PSF의 최대값 위치나 무게중심을 강제로 중앙 이동하는 연산이 아니다. 기존 Lens에 붙이는 서비스는 불변 부모와 Factory 원본, 광학 가정·훈련 계보·적용 조건을 보존해야 한다.

현재 조합 등록은 다음 적용 검사를 거친다. 새 PSF를 붙이는 행위만으로 기존 RI·입사각 응답의 유효 범위를 넓히지 않는다.

- 부모의 모든 숫자 `valid_domain`에 대해 적용 조건이 있어야 하며, 단위가 같고 범위 안에 들어가야 한다. 누락·Unknown·범위 밖은 등록을 거부한다. 통과한 숫자 조건은 해당 값의 고정 범위로 기록한다.
- 부모의 `factory_application_conditions`가 있으면 문자열 조건을 포함해 정확히 보존한다. `light_id`의 변경·누락도 다른 적용 조건이다.
- PSF 파장 범위의 양 끝은 부모의 nm 범위와 정확히 같아야 한다. 부모가 파장별 RI/입사각 표를 가지면 중간 파장까지 전체 좌표가 동일해야 한다. 현 등록 경로는 부분 범위 축소나 파장 재표본화를 자동 수행하지 않는다. 필요한 경우 출처를 보존하는 별도 모델 변환이 먼저 필요하다.
- 적용 조건 또는 부모 Properties에 `f_number`가 명시되어 있으면 단위 `1`과 값이 PSF 요청의 F-number와 같아야 한다. 이미 알려진 불일치를 “독립 동공 가정”이라는 설명으로 무시하지 않는다.

결합 모델은 `source_kind=estimated`, 불확실도 `unknown`으로 남고 부모와 훈련 데이터 계보를 이어받는다. 이러한 조건 검사는 서로 다른 방식으로 얻은 PSF·RI·각도 분포가 같은 실물의 결합 응답이라는 증명은 아니다.

복소 OTF는 중심을 맞춘 최종 이산 Kernel의 DFT다. 실수부와 허수부를 모두 보존한다. PSF가 비대칭이면 허수부·위상이 의미를 가지므로 MTF 절댓값만으로 대체하면 안 된다. **이 OTF에는 유한 범위 정규화와 수신 Pixel aperture가 포함되어 있으며 순수 Lens optical MTF나 RAW/ISP SFR가 아니다.** 파장마다 DC는 수치 허용 범위 안에서 1이다.

| Artifact | 축 / 단위 |
| --- | --- |
| `psf_bin_energy` | `[configuration,wavelength,y,x]`, 입력 동공 에너지 대비 Pixel Bin 비율 `1` |
| `psf_kernel` | `[wavelength,y,x]`, 마지막 설정의 유한 범위 정규화 Kernel `1` |
| `retained_energy_fraction` | `[configuration,wavelength]`, 정규화 전 에너지 합 `1` |
| `kernel_otf_real`, `kernel_otf_imag` | `[wavelength,frequency_y,frequency_x]`, 정규화 Kernel의 복소 DFT 성분 `1` |
| `pupil_amplitude` | 마지막 동공 설정의 `[pupil_y,pupil_x]` 이진 진폭 `1` |
| `pupil_opd_nm` | 같은 동공 설정의 광로차 `nm`; 동공 외부는 0으로 표시 |

Kernel 좌표는 `(index-kernel_size//2)*pixel_pitch_um`, 주파수 좌표는 `fftshift(fftfreq(kernel_size, d=pixel_pitch_um))` cycles/µm이다. cycles/mm로 표시하려면 1000을 곱한다. 동공 좌표는 `(index-samples//2)*(1.2*pupil_diameter_mm/samples)` mm이며 Artifact의 동공 `samples`는 마지막 비교 설정 값이다. 축·단위는 Summary의 원본 요청과 설정 목록으로 재구성하며, 원본을 삭제한 단독 배열만으로 적용 범위를 추정하지 않는다.

## 독립 구현 검증과 남은 범위

[test_psf_factory.py](../tests/platform/test_psf_factory.py)는 Prysm 함수나 이산 동공 FFT를 기준값으로 재사용하지 않고 다음을 확인한다.

- 무수차 원형 동공의 연속 Airy Intensity를 Gauss–Legendre 적분하여 **정규화 전 Pixel 에너지**를 비교한다. 무한 평면에서 에너지가 1인 식을 사용하며 유한 정사각형 영역은 내접·외접 원의 해석적 Encircled energy 사이에 있어야 한다.
- 동공 격자와 수신 격자를 각각 정밀하게 만들 때 Airy 기준 오차가 감소하는지, 작은 영상 범위나 과도하게 엄격한 허용치가 후보 생성을 막는지 확인한다.
- Defocus의 중심 에너지 감소, Astigmatism 부호 반전의 축 교환, Coma 부호 반전의 좌우 대칭과 복소 OTF 위상 반전을 시험한다.
- 직접 복소 지수 합과 OTF를 비교하고, 역변환으로 PSF를 복원한다. 동일 요청의 결정성과 원본 불변, 잘못된 파장·단위 의미·Nonfinite·격자·주기 복제·예산·거대 정수 입력 거부도 확인한다.
- 조합 시 부모의 온도·노출·Gain 범위나 고정 광원 조건을 삭제·확대할 수 없고, 파장 Coverage·중간 좌표·F-number가 서로 모순되면 거부되는지 확인한다. 성공한 조합은 부모 응답표·훈련 계보를 변경하지 않는다.

위 독립 Airy 예시의 마지막 설정은 500/600 nm에서 유한 범위 에너지 차이 5e-5 미만, 정규화 Kernel L1 차이 0.0024 미만으로 해석적 Pixel 적분과 일치한다. 이 숫자는 이 명시된 합성 시험의 관측 결과이며 다른 F-number·파장·수차의 오차 경계로 사용하지 않는다.

실제 Lens Wavefront 추출·동공 진폭·시야별 수차·거리·색수차·편광, PSF와 입사각 응답의 결합 분포, 광학 꼬리의 정량 전파, 실측 PSF/MTF/SFR와의 독립 비교는 남아 있다. `PROPOSED`와 소프트웨어 시험 통과는 승인·독립 실측·상용제품 완료를 뜻하지 않는다.

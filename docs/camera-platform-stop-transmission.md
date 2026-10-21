# 조리개 투과율 · 회절 · 절대 광량 연결

`0.16.0.dev2` 소스 개발판은 명시한 수동 조리개(stop) 투과율을 광선 광량과
PSF에 함께 적용합니다. 원본 처방, 투과율, 광량 적분, PSF, 장면 투영, RAW를
불변 근거로 연결합니다. 기본 설치본 v0.15의 변경이나 실물 렌즈 검증을 뜻하지 않습니다.

## 계산 경계

투과율 `T(파장, stop 반경)`는 power 비율입니다. 실제 광선이 교차한 stop의
좌표에서 값을 평가합니다. 역방향 광량 계산에는 `T`, 복소 동공 진폭에는 `sqrt(T)`를
곱합니다. PSF는 투과한 동공 power에 대해 조건부로 정규화하고, 유한한 PSF 영역의
누락 에너지는 별도 검사합니다.

회절 진폭의 정의는 [Prysm 회절 모델](https://prysm.readthedocs.io/en/latest/tutorials/First%20Diffraction%20Model.html),
광량 적분의 정의는 [PBRT radiometric integrals](https://pbr-book.org/4ed/Radiometry%2C_Spectra%2C_and_Color/Working_with_Radiometric_Integrals)를
따릅니다. 여기서 power 투과율의 제곱근을 진폭에 곱하는 관계를 도출했습니다.

단위 합 PSF와 상대 조도만으로는 균일 필터의 광량 감소를 표현할 수 없습니다.
새 Lens 응답의 `radiometry`에는 파장·상고별 절대 `E/L [sr]`을 보존합니다.
새 장면 투영은 `E_ideal = L * (E/L)_on_axis`를 계산하며 추가 `T`나 `pi/(4N²)`를
곱하지 않습니다. 이후 기존 Runtime이 조건부 PSF, 상대 조도, 입사각 가중 QE를 적용합니다.
기존 CFA·수집효율과 pixel-integrated PSF의 aperture도 다시 곱하지 않습니다.

## 버전과 재사용 계약

- 기존 plugin ID와 v1 요청의 계산 의미는 유지됩니다.
- `lens_prescription_v2`는 `stop_transmission`을 필수로 포함합니다.
- `prescription_field_psf_v2`는 동일한 v2 처방을 그대로 내장합니다.
- `camera_stop_transmission_v1`은 stop ID, 정확한 파장 축, 0–1 반경 축,
  `[파장, 반경]` power 투과율, power 기준 선형 보간, 출처를 보존합니다.
- PSF와 광량 Factory의 전체 처방이 다르면 모델 결합을 거부합니다. 이 모델에는
  독립적인 원형 pupil PSF를 붙일 수 없습니다.
- `prescription_radiometry_projection_v1`은 Lens·두 Factory·투과율 hash와
  계산된 on-axis 계수를 고정합니다. 이 Lens를 기존 수동 투과율 경로로 우회할 수 없습니다.
- 실행, 캐시 재사용, 결과 등록 때도 원본 파일과 계보를 확인합니다. 손상되거나 누락된
  광량·PSF 근거를 정상 캐시로 대체하지 않습니다.

## 사용 흐름

Factory 화면의 **동공 투과율 포함 형식**을 누르면 v2 입력을 불러옵니다.
파장별 반경 투과율과 실제 설계 출처를 확인하고, 같은 처방으로 광량·PSF를 계산합니다.
광량 모델을 등록한 뒤 해당 모델을 PSF의 부모로 선택합니다. 결합 Lens로 카메라를
구성하면 3D 장면의 투영 계수가 Factory 근거에서 자동으로 채워집니다.
화면에서 그 계수는 읽기 전용이며, 다른 값은 새로운 렌즈 모델로 계산해야 합니다.

```python
psf = client.factory_template("prescription_psf_v1", request_version=2)
mask = psf["request"]["prescription"]["stop_transmission"]
mask["power_transmittance"] = [[0.25, 0.25] for _ in mask["wavelength_nm"]]
mask["source"] = {"kind": "estimated", "description": "명시한 균일 25% 수동 필터"}

ray = client.factory_template("rayoptics_lens_v1", request_version=2)
ray["request"] = psf["request"]["prescription"]
# 두 작업을 제출한 뒤, 수치 판정을 확인하고 같은 운용 조건으로 모델을 등록합니다.
```

같은 광선 격자·파장의 저장 광휘는 다른 투과율의 Lens에 재사용할 수 있습니다.
초점거리·센서 격자·장면 조건을 바꾸면 기존 재사용 제한이 계속 적용됩니다.

## 검증과 남은 범위

균일 `T=1`의 기존 배열 일치, `T=.25`의 PSF 불변·광전자량 ¼, 독립적인 연속
광량 적분·Hankel 회절 적분, 실제 stop 교점, 수렴·tail·cone·mapping 제한,
잘못 연결한 부모와 손상된 근거의 거부를 검사합니다. 실제 HTTP/Worker 검토는
`tools/verify_camera_stop_transmission.py`가 별도 QA 상태에서 수행합니다.

이번 모델은 중심 정렬 평면 stop의 수동·회전 대칭·파장별 투과율입니다. 각도·편광에
따른 coating/Fresnel 계산, 일반 pupil Jacobian 전달, 비축·비구면·유한 물체거리,
4개 native 픽셀 site와 방위각 응답의 결합은 남아 있습니다. 현재 장면 경로는 pinhole
광휘와 근축 PSF를 결합하는 근사이며 깊이 의존 광학 전달을 해결하지 않습니다.
수치 판정은 총 예측 불확실도나 실물 정확도의 보증이 아닙니다.

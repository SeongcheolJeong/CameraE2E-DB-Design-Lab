# p0014 렌즈와 저장 Sensor DB의 native 응답 데이터 요청

`camera_platform.native_sensor_readiness.native_sensor_data_request()`는 저장된 Sensor DB revision과 실제 PSF·joint 방향 응답의 계약을 대조한다. 결과는 **원본 데이터 확보 요청**이며, 기존 estimate나 LUT를 새로운 EQE로 변환하지 않는다. 선택한 revision의 결과 SHA-256, 로컬 QE/FDTD/TCAD 파일 SHA-256, 정확히 필요한 축과 물리량, 차단 사유를 반환한다. 입력 Lens PSF와 방향 payload의 수치 계약을 확인하지만 Factory 출판 권한·광학 정확도는 별도 검증이다.

```python
from camera_platform.native_sensor_readiness import native_sensor_data_request

request = native_sensor_data_request(
    "DEF-1803-805",
    psf=lens_model["payload"]["psf"],
    directional_response=lens_model["payload"]["directional_response"],
    repository_root="/path/to/CameraE2E",
)
assert request["status"] == "source_data_required"
```

등록된 Factory Lens를 사용하는 Agent와 노트북은 서버의 작업 공간과 Lens 출처를 고정하는 SDK 경로를 사용할 수 있다. `lens_model_id`는 PSF Factory 실행을 검증한 Lens 모델이어야 한다. 임의의 PSF JSON이나 서버 파일 경로는 이 API에 전달하지 않는다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient() as client:
    request = client.native_sensor_data_request(
        lens_model_id="<registered-factory-lens-model-id>",
        sensor_identifier="DEF-1803-805",
    )
    assert request["registered_lens_binding"]["model_id"]
    assert request["product_ranking_allowed"] is False
```

같은 읽기 전용 요청은 `GET /api/v1/native-sensor-data-requests?lens_model_id=...&sensor_identifier=...`로 조회한다. 특정 저장 simulation을 고정할 때는 `run_id`도 제공한다. 이 경로는 데이터 확보 요구를 설명하며 EQE 생성이나 Lens×Sensor 결합 실행을 수행하지 않는다.

2026-09-28 로컬 감사 결과는 [기계 판독 보고서](../reports/camera_platform/integrated-workflows-20260928/dev33-native-sensor-readiness.json)에 원본 경로·해시와 함께 고정했다. p0014 제한 대역 후보는 550/650 nm, PSF sampling pitch 3 µm, 실제 양의 광량을 가진 저장 ray의 최대 입사각 약 8.091°이다. 이 광학 후보 자체는 `PROPOSED`이며 전체 RGB 렌즈 모델이나 물리 검증 결과가 아니다.

현재 Sensor DB의 *current* 저장 simulation 144개 중 3 µm 및 R/Gr/Gb/B인 revision은 10개다. 예시 `DEF-1803-805`는 3 µm, 400–700 nm의 네 사이트를 갖지만 `physical_estimate_v1`이다. nominal QE는 검증되지 않은 microlens routing loss를 적용하지 않으며 charge collection 계수는 가정이다. readout도 `unmeasured_assumption`으로 분류된다. 따라서 550/650 nm 두 점이 저장 스펙트럼에 존재하는 것만으로 angular collected EQE가 생기지 않는다. 저장 runtime snapshot은 현재 코드와 호환되어 기존 uniform-scene 계산은 가능하지만, p0014의 joint 방향·PSF와 이 estimate를 곧바로 물리 결합하는 근거는 아니다.

카탈로그 QE JSON 224개는 native θ×φ×site grid를 제공하지 않는다. 생성된 TCAD profile 두 트리에는 각각 216개가 있으나 모든 `calibration_status.is_measured`와 `geometry_measured`가 `false`인 metadata proxy다. 센서별 solver-v3 FDTD response 세 개는 2.9 µm, 각각 0/4030 sample 완료, `unverified`이며 입사 광량의 기준이 native 독립 픽셀이 아닌 **전체 2×2 cell**이다. 따라서 이 흡수 응답에 임의의 4배·수집 계수를 적용해 3 µm p0014 센서의 공간적으로 독립된 absolute collected EQE로 취급하면 정규화, 구조, collection의 의미가 바뀐다.

`DEF-2206-805`(IMX662)를 선택하면 추가로 **pitch 불일치**가 발생한다. 저장 센서의 native pitch는 2.9 µm이고 p0014 PSF sample pitch는 3.0 µm이다. 요청서는 IMX662의 EQE를 2.9 µm 원래 revision에 묶어 요구하며, 3.0 µm 센서 데이터로 바꿔 적지 않는다. 결합 계산 전에는 3.0 µm인 다른 센서 revision을 선택하거나, 해당 2.9 µm 센서에 맞는 광학 PSF를 새로 생성·검증해야 한다. 광학 재표본화 방식을 택한다면 원본 렌즈의 복소 pupil/광량·PSF 에너지·공간 지원 계약을 보존하는 검증이 먼저 필요하다. 임의의 PSF 보간이나 센서 ID만 바꿔 붙이는 것은 허용하지 않는다.

실제 결합을 하려면 동일 센서 component/revision에 대해 다음 원본이 필요하다.

1. 3 µm native RGGB의 독립적인 R/Gr/Gb/B 사이트 각각에 대한 **수집 전자 수 / 해당 픽셀에 입사한 광자 수**의 절대 응답. CFA, microlens, Si absorption, charge collection이 한 번씩 포함되어야 한다. Generation/absorption만 있으면 별도로 검증된 collection 모델이 필요하다.
2. 현재 결합 경로에서는 렌즈와 정확히 같은 `[550, 650]` nm 축, θ=0부터 유효 ray 최대각 이상, 0–360° 주기적 φ 표본(최대 간격 90°), unpolarized 조명, `sensor_surface_before_pixel_optics` 기준면과 `propagation_into_sensor_x_column_y_row` 방향 정의가 필요하다. θ=0의 응답은 모든 φ에서 같아야 한다. 이 축은 현재 **제한 대역**만 지원한다.
3. 측정이라면 원본 raw measurement·calibration·geometry, solver라면 solver output·geometry·materials·collection 원본을 해시로 고정하고, 절대 정규화·사이트 좌표·축 커버리지·독립 사이트 의미·collection 중복 방지·불확도 또는 수치 수렴 근거를 각각 판정해야 한다. 원본 해시와 작성자 qualification은 검증 가능한 입력 계약이지만, 독립적인 카메라 실측 정확도 입증은 아니다.
4. 같은 component/revision의 readout profile과 전기 가정의 출처를 고정해야 한다. 현재 저장 DB readout의 공통 가정으로 제품별 전기 성능을 순위 매길 수 없다.

이 요청은 **독립 사이트** 모델에 한정한다. 주변 픽셀로 재분배되는 공간 crosstalk나 센서 MTF까지 이 4-site LUT 하나로 표현하지 않는다. 그런 성능을 주장하려면 별도의 입력-출력 사이트 전달 모델과 검증이 필요하다.

이 원본이 생기면 `native_response_assets`의 bundle 검사와 `native_response_publication`의 draft Sensor 출판 경로에 넣을 수 있다. 그때도 source-reported qualification과 물리 검증, 상용 제품 순위를 구분한다. p0014 광학 후보 역시 전체 설계 대역·공차·실측 검증을 거쳐야 제품 판단에 사용할 수 있다.

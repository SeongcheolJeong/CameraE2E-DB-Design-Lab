# RAW Measurement / Observation Adapter

`src/camera_platform/measurement.py`는 실제 취득 RAW와 시뮬레이션 RAW에 동일하게
적용할 수 있는 ROI 관측 연산자입니다. RAW의 R/G·B/G를 계산하고, 어떤 센서 위치와
CFA 표본을 평균했는지 함께 반환합니다. Hardware Driver, RAW Decoder, 계측기 교정,
Camera 응답의 선형성 검증은 이 모듈의 기능이 아닙니다.

함수에 전달하는 `source_kind="measured"`는 호출자가 선언한 출처입니다. 분석
성공이나 ADC 범위 검사만으로 실제 취득·계측 정확도·실물 검증 PASS를 주장하지
않습니다. 플랫폼 서비스가 원본 NPY 바이트, 취득 Metadata, 실물/장비 ID, 시험
Revision과 분석 결과를 연결해 보존합니다.

## 공개 함수

```python
from camera_platform.measurement import (
    analyze_raw_sequence,
    observation_method,
    observation_sampling_weights,
    observe_field_response,
)

# Actual pixel observations; dark_frames is None or another [frame,row,col] stack.
result = analyze_raw_sequence(raw_frames, dark_frames, specification)
measurement = result["measurement"]
diagnostics = result["diagnostics"]

# Operator-only validation/fingerprint; does not establish acquisition validity.
method = observation_method(specification, raw_frames.shape, dark_present=False)

# Apply the same CFA/ROI sample positions to an expected radial response.
sampling = observation_sampling_weights(specification, raw_frames.shape, field_radius)
expected = observe_field_response(field_channel_signal_dn, sampling)
```

`analyze_raw_sequence()`는 입력 배열을 수정하거나 RAW 비트를 재해석하지 않습니다.
실패 시 `ObservationError(ValueError)`의 `.issues`에 `code`, `field`, `message`,
`severity` 및 필요한 표본 수/클리핑 수 등을 담습니다. 대표 거부 사유는
`ADC_CLIPPING`, `DARK_CONDITION_MISMATCH`, `DARK_SHAPE_MISMATCH`,
`INSUFFICIENT_CFA_SAMPLES`, `NONPOSITIVE_CHANNEL_SIGNAL`, `OUT_OF_DOMAIN`,
`RESOURCE_LIMIT`입니다.

## 입력 예제

아래 수치는 함수 계약을 설명하는 **해석식 Fixture**입니다. 실제 센서 크기,
광원, 장비 또는 측정값을 의미하지 않습니다. 실제 사용 시 고정된 카메라·시험과
일치하는 값을 넣어야 합니다.

```python
specification = {
    "source_kind": "simulated",
    "source_uri": "fixture://raw-observation-example",
    "raw_format": {
        "cfa": "RGGB",
        "bit_depth": 12,
        "black_level_dn": 64,
        "response_type": "linear",
    },
    "geometry": {
        "kind": "native_sensor",
        "full_sensor_shape": [48, 64],
        "crop_origin": [5, 9],
    },
    "rois": [
        {"id": "inner", "field_radius": 0.25, "box": [8, 8, 8, 8]},
        {"id": "outer", "field_radius": 0.75, "box": [0, 0, 8, 8]},
    ],
    "conditions": {
        "exposure_s": {"value": 0.004, "unit": "s"},
        "temperature_c": {"value": 25.0, "unit": "degC"},
        "gain": {"value": 1.0, "unit": "1"},
        "light_id": {"value": "analytic-example", "unit": "1"},
    },
    "included_effects": [
        "sensor_effective_qe", "mean_dark_charge", "readout_clipping"
    ],
    "uncertainty": {"kind": "unknown"},
}
```

이 예제는 `[frames,16,24]` 정수 배열을 관측할 수 있습니다. Native RGGB 위상은
전체 센서의 `[0,0]`을 기준으로 합니다. Crop이 `[5,9]`처럼 홀수 위치에서 시작해도
원래 센서의 CFA 위상을 유지합니다. Local crop의 왼쪽 위를 다시 R로 가정하지
않습니다. `crop_origin`과 ROI `box`의 순서는 각각 `[row,col]`,
`[row,col,height,width]`이며 인덱스는 0부터 시작합니다.

실제 Capture에서는 장비에 명령한 설정뿐 아니라 실제 적용된 Exposure/Gain/온도와
광원 식별자를 등록해야 합니다. 이 모듈은 그 Metadata를 보존하지만 명령값과 실제
설정의 일치를 장비에서 읽어 확인하는 기능은 없습니다. `included_effects`는
연결하는 Test의 특성 정의와 같아야 합니다.

## 배열과 Geometry 계약

| 항목 | 지원 범위 |
| --- | --- |
| RAW 배열 | NumPy 정수 dtype, `[frames,rows,cols]`, 1–256 frames |
| Code 범위 | 명시된 1–24 bit ADC의 정수 DN, `0..2^bit_depth-1` |
| Spatial shape | 짝수 rows/cols, 한 프레임 최대 4,194,304 pixels |
| 입력 크기 | Light와 Dark 배열의 논리적 `nbytes` 합계 최대 64 MiB |
| CFA/Readout | RGGB, 선형, unpacked Bayer, 한 HDR stream만 지원 |
| ROI | 1–32개, 영상 안에 완전히 포함, R/G/B 각각 최소 16 sites |
| ROI 축 | 고유한 ID, 0–1 범위에서 엄격히 증가하는 고유한 nominal field label |

Float 배열, NaN, 값 범위를 넘는 Code, 암시적인 Bit scaling, Log/HDR/전처리
Tone curve, 임의 회전/Resizing은 받아들이지 않습니다. 외부 RAW 파일의 Unpacking,
Byte order 처리, 제조사별 Linearization 등은 별도 이름과 버전이 있는 Decoder가
먼저 수행하고 원본과 변환 이력을 보존해야 합니다. `black_level_dn`은 DN 단위이며
채널별 Black pedestal은 이 버전에서 지원하지 않습니다.

Geometry는 다음 두 종류를 구분합니다.

- `native_sensor`: 원래 센서의 전체 shape와 실제 crop origin을 사용합니다.
  Square pixel, unbinned native 좌표를 전제로 하며 crop은 센서 영역 안에 있어야
  합니다. Full sensor shape와 crop의 Aspect ratio가 다를 수 있지만 Resizing은
  하지 않습니다.
- `representative_normalized_field_raster`: Runtime이 만든 대표 시야 Raster를
  관측합니다. `raster_shape`가 입력과 정확히 같고
  `mapping="center_to_corner_radius_v1"`이어야 합니다. Native sensor Capture와
  같은 Geometry로 바꾸어 표시하지 않습니다.

Native pixel 좌표 `(row,col)`의 반경은 전체 센서 중심에서의 거리로 계산합니다.
센서 모서리 pixel center의 반경이 1이고, 분모는 센서의 반대 모서리까지의 거리를
사용하지 않습니다.

```text
r = hypot(row - (full_rows-1)/2, col - (full_cols-1)/2)
    / hypot((full_rows-1)/2, (full_cols-1)/2)
```

ROI의 `field_radius`는 **명목상 구간 Label**입니다. 실제 관측은 ROI 안의 여러
CFA 위치를 평균합니다. 반환하는 `sample_geometry.rois`에는 실제 반경의 min/mean/max,
R/G/B pixel 수, 관측 DN 범위와 Black/Dark 제거량이 있습니다. Label을 그대로
점 응답 Curve에 대입해 비교하면 ROI 평균과 점 응답을 혼동하게 됩니다.

## Black/Dark 처리와 Ratio 정의

Dark 배열을 주지 않으면 각 CFA 관측값에서 선언된 Black level만 제거합니다.
이 경우 `dark_correction="black_level_only"`이며 평균 Dark charge는 남습니다.

Dark 배열을 주면 동일 좌표/형식에서 취득한 Dark stack의 프레임 평균을 제거합니다.
Dark 평균에 이미 Black pedestal이 포함되어 있으므로 Black을 다시 빼지 않습니다.
Light와 Dark의 프레임 수는 달라도 되지만 공간 shape는 같아야 합니다.
`dark_conditions`에 실제 Exposure/Gain/온도를 명시하고 Light와 정확히 일치해야
합니다. Exposure가 다른 Dark를 자동 Scaling하지 않습니다.

```python
specification["dark_conditions"] = {
    "exposure_s": {"value": 0.004, "unit": "s"},
    "temperature_c": {"value": 25.0, "unit": "degC"},
    "gain": {"value": 1.0, "unit": "1"},
}
result = analyze_raw_sequence(raw_frames, dark_frames, specification)
```

각 ROI에서는 먼저 CFA별 공간 평균과 프레임 평균을 구하고 Dark/Black을 제거한
`mean_R`, `mean_G`, `mean_B`를 얻습니다. 최종 값은 `mean_R / mean_G`,
`mean_B / mean_G`입니다. Pixel별 또는 프레임별 Ratio를 먼저 평균하는 방식이
아닙니다. Gr/Gb는 하나의 G로 합칩니다. 두 Green site의 서로 다른 응답을 별도로
구분하는 모델은 후속 계약이 필요합니다.

보정 후 채널 평균이 0 이하이면 Ratio를 만들기 위해 Clamp하지 않고 거부합니다.
관측 ROI의 Light 또는 Dark Code에 ADC 하단 0 또는 상단 White가 하나라도 있으면
클리핑된 관측으로 거부합니다. ROI 밖 클리핑은 전체 입력 통계로 공개하지만 해당
ROI 평균에 포함시키지 않습니다. 겹치는 ROI가 있으면 진단의 총 표본 수에는 같은
pixel이 다시 집계되며, 이를 독립 표본 수로 해석하면 안 됩니다.

ADC 범위 안의 Code만 있다는 사실은 Hardware Well clipping이 없다는 증거가
아닙니다. Gain 또는 전처리 때문에 Full-well saturation이 ADC 상단보다 아래에서
발생할 수 있습니다. 따라서 `physical_well_clipping_verified`는 false이며,
실물 선형성/포화 검증은 별도 취득 근거가 필요합니다.

## 측정값과 불확실도

결과의 `measurement`는 다음 정보를 제공합니다.

- `metric_id="raw.channel_ratio"`, `unit="1"`, `reference_plane="sensor_raw"`.
- `values`: ROI 순서대로 R/G, B/G를 펼친 배열.
- `axes`: `field_radius` 명목 Label 다음 `channel_ratio=["R/G","B/G"]`.
- `channel_means_dn`: `[ROI][R,G,B]`의 보정된 DN 평균.
- `conditions`, `included_effects`, `source_kind` 및 전달된 원본/실물/계측기 정보.
- `observation_method`, `sample_geometry`, `uncertainty`.

기본 `measurement.uncertainty`는 unknown입니다. Light와 사용한 Dark가 각각
3 frame 이상이면 `diagnostics.statistical_uncertainty`에 Ratio의 경험적 Standard
Error를 제공합니다. 시간 변화가 있는 채널별 ROI 평균의 공분산을 프레임 수로
나누고, Dark 공분산을 더한 뒤 Ratio의 1차 미분으로 전파합니다.

```text
Cov(mean_corrected) = Cov(light frame channel means)/N_light
                   + Cov(dark frame channel means)/N_dark   [when dark exists]

SE(R/G) ≈ sqrt(gradient(R/G)^T * Cov(mean_corrected) * gradient(R/G))
```

이는 Light/Dark Capture가 독립이고 시간적으로 안정적이며 Ratio 선형화가
적절하다는 가정을 사용합니다. 장비 교정, Drift, 공간 고정 패턴의 대표성, 모델
오차, Systematic bias를 모두 포함한 결과가 아닙니다. Standard Error는 Confidence
Interval도 Absolute bound도 아닙니다. 표본 분산이 0이더라도 물리적 불확실도를
0으로 설정하지 않습니다. 프레임 수가 부족하면 해당 Standard Error 값은 null로
남깁니다.

외부에서 이미 성립시킨 절대 오차 경계를 제공하려면 다음처럼 Ratio 수와 같은
길이의 비음수 값을 선언합니다.

```python
specification["uncertainty"] = {
    "kind": "absolute_bound",
    "unit": "1",
    "values": [0.02, 0.02, 0.03, 0.03],
    "method_and_version": "externally-reviewed-error-budget/v1",
    "source_uri": "registered-evidence://replace-with-actual-budget",
}
```

위 숫자와 URI도 형식 예제입니다. Adapter는 단위·배열 정합과 근거 필드 존재를
검사하고 선언된 경계를 보존합니다. 외부 오차 예산의 타당성을 자동 증명하지
않으며, 경험적 Standard Error를 이 필드로 자동 승격하지 않습니다.

## 같은 연산자를 예측에 적용하기

`observation_method`는 다음 공통 연산자 정보를 정규화하고 Hash로 고정합니다.

```text
id: raw_roi_observation_v1
dark_correction: black_level_only | mean_dark_frame
specification:
    raw_format / geometry / rois
    minimum_cfa_pixels: 16
    dark_correction
    saturation_policy: reject_adc_endpoints_in_rois
sampling_hash: SHA-256 of canonical operator specification
```

출처·실물·장비·실제 프레임 수는 연산자 Hash에 섞지 않습니다. 그 정보는 관측의
Metadata로 별도 보존하므로 실측과 예측에 같은 연산자를 엄격히 적용할 수 있습니다.
`observation_method()`는 Pixel을 읽지 않고 연산자 정의를 다시 생성하므로, 저장한
Test의 정의가 실제 Sampling 설정과 같은지 확인할 때 사용합니다.

`observation_sampling_weights()`의 반환값은 다음과 같습니다.

```text
mode: raw_roi_channel_mean_ratio
field_radius: [model radial basis coordinates]
roi_field_radius: [nominal ROI field labels]
roi_ids: [ROI IDs]
channel_field_weights: [ROI][R,G,B][model field basis]
content_hash: SHA-256 of these sampling values
```

각 `[ROI,channel]` 가중치 합은 1입니다. Native crop origin에 따른 CFA 위상과
전체 센서 좌표를 사용하여 실제 pixel 위치에서 선형 보간 Basis를 평균합니다.
Relative Illumination, QE 또는 다른 물리 응답은 가중치에 포함하지 않습니다.
Model basis는 2–256개의 증가하는 0–1 좌표여야 하며, 모든 ROI 표본 반경을
덮어야 합니다. 범위 밖 표본을 끝점에 Clamp하지 않습니다.

```text
observed_channel_mean[roi,channel]
    = sum(channel_field_weights[roi,channel,field]
          * expected_channel_signal[field,channel])
```

`observe_field_response()`는 이 가중치를 `[field,3]`의 기대 응답에 적용하여 채널
평균과 Ratio를 반환합니다. 입력은 관측과 동일한 Black/Dark 처리 단계에 있어야
합니다. 특히 Runtime의 `channel_reference_signal_dn`은 기대 Dark를 제거한 값이므로,
Black-only RAW 관측과 비교할 때는 같은 Readout의 기대 Dark DN을 먼저 더해야
합니다. Dark 평균 제거 관측에서는 그 값을 그대로 사용할 수 있습니다.

이 연산자는 ROI Geometry의 불일치를 해결합니다. 연속 기대값과 실제 ADC 반올림,
유한 프레임 관측, 미확정 모델 오차 사이의 차이는 여전히 정량화해야 합니다.
Geometry가 같다는 이유만으로 예측 불확실도가 해결되거나 실물 검증이 완료되는
것은 아닙니다.

## 구현 확인

```sh
python -m pytest tests/platform/test_measurement.py -q
```

검사는 홀수 Crop의 CFA 위상, 같은 Native 좌표의 보간 가중치, 점 응답과 ROI 평균의
차이, 실제 정수 RAW와 연속 기대값의 관측 정합, Black/Dark의 단일 제거,
표본 공분산 기반 Standard Error, Clip/형식/단위/조건/크기 거부를 확인합니다.
사용한 배열은 명시적인 해석식 Fixture이며 실제 장비의 정확도를 검증한 결과가
아닙니다.

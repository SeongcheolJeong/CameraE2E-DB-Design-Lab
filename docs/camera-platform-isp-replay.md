# 보존 RAW의 명시적 ISP 재처리

`camera_platform.isp`는 기존 정수 RAW에 고정 ISP 설정을 적용한다. 입력 코드를 변경하거나 새 센서 전하·잡음·노출을 생성하지 않는다. 같은 RAW의 ISP 설정 비교와, 새로 시뮬레이션한 RAW의 후단 처리가 이 함수를 공유할 수 있다. **재처리는 센서·노출·Gain을 바꾼 재촬영이 아니며, 색 정확도나 특정 상용 ISP의 물리 검증도 아니다.**

## 호출 계약

```python
from camera_platform.isp import default_profile, process_raw, validate_profile

profile = default_profile()
profile["white_balance"]["gains_rgb"] = [1.4, 1.0, 1.2]
profile["ccm"]["matrix_rgb"] = [
    [1.05, -0.05, 0.0],
    [0.0, 1.0, 0.0],
    [-0.05, 0.0, 1.05],
]
profile = validate_profile(profile)
result = process_raw(
    raw_frames,
    {
        "cfa": "RGGB",
        "bit_depth": 12,
        "black_level_dn": 64,
        "response_type": "linear",
        "packing": "unpacked",
        "layout": "bayer",
        "hdr_streams": 1,
    },
    profile,
    geometry={
        "kind": "native_sensor",
        "full_sensor_shape": [1080, 1920],
        "crop_origin": [1, 1],
    },
)
# raw_frames must already be the declared bounded crop, not resized full-frame data.
rgb_frames = result["arrays"]["rgb_frames"]
```

위 튜닝 숫자는 API 사용을 보이는 예시이며 보정된 카메라 계수가 아니다. `validate_profile()`은 정규화된 독립 사본을 반환한다. 미지원·누락·잘못된 입력은 `ISPError(ValueError)`와 구조화된 `issues`로 거부한다. `describe_capabilities()`는 지원 단계와 자원 한도를 반환한다.

RAW는 `[frames, rows, columns]` 또는 단일 `[rows, columns]` NumPy 정수 배열이다. 8/16/32-bit 저장 정수, ADC 1–24 bit의 비음수 코드, 명시적 선형 unpacked Bayer RGGB·단일 Stream만 지원한다. 0부터 `2**bit_depth-1` 밖의 코드를 자동 클리핑하지 않는다. Optional `white_level_dn`은 ADC 최대값, `black_level`은 `black_level_dn`과 일치해야 한다. Packed RAW, Float/Boolean/Object/64-bit 정수, 알 수 없는 전처리·취득 파라미터를 거부한다. 원본 NPY·취득 파일 보존과 해시 검증은 서비스의 책임이다.

## 고정된 버전별 그래프와 수치 의미

`default_profile()`의 전체 명세는 다음과 같다. 순서 변경이나 임의 단계 추가를 허용하는 일반 ISP Graph는 아직 아니다.

```json
{
  "schema": "camera_isp_profile_v1",
  "graph": ["black_level", "demosaic", "white_balance", "ccm", "tone"],
  "precision": "float64",
  "black_level": {"source": "raw_format", "negative_policy": "preserve"},
  "demosaic": {"method": "bilinear", "boundary": "reflect"},
  "white_balance": {"mode": "fixed", "gains_rgb": [1.0, 1.0, 1.0]},
  "ccm": {
    "matrix_rgb": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    "orientation": "output_rows_input_columns"
  },
  "tone": {"curve": "srgb", "clip_input": [0.0, 1.0]},
  "disabled_effects": ["auto_white_balance", "lens_shading_correction", "hdr_merge"]
}
```

| 단계 | 실제 연산과 제한 |
| --- | --- |
| BLC | `(raw_dn-black_level_dn)/(2**bit_depth-1-black_level_dn)`. Black subtraction은 한 번이며, 아래쪽 음수를 보존한다. 프레임 최댓값·평균에 따른 정규화가 없다. |
| Demosaic | 실제 `pyisetcam.ip._ie_bilinear`를 사용한다. RGGB 샘플의 선형 Bilinear 보간이며 Crop 경계는 반사 연장한다. 엔진 내부 함수 의존성은 소스 해시와 회귀시험으로 고정한다. |
| 고정 WB | RGB별 양의 배율 `[1/16,16]`. 자동 광원 추정·AWB 상태가 없다. |
| CCM | `rgb_out = rgb_in @ matrix_rgb.T`. 행은 출력 채널, 열은 입력 채널이다. 계수는 `[-8,8]`, 행별 절댓값 합은 16 이하. 음수 계수를 허용하고 출력 음수와 1 초과 값을 보존한다. Matrix의 측정·색공간 타당성을 숫자 범위 검사로 인증하지 않는다. |
| Tone | CCM 결과를 명시적으로 `[0,1]`에 클리핑한 뒤 `linear` 또는 실제 `pyisetcam.utils.linear_to_srgb`를 적용한다. sRGB는 전달함수만 뜻하며 표준 색원색·백색점·색 정확도를 보장하지 않는다. |

모든 처리는 IEEE754 Float64이며 Output RGB도 Float64이다. 정수 출력 양자화·고정소수점 연산·HW ISP 등가성은 제공하지 않는다. 기본 프로필은 기존 pyisetcam의 Bilinear·unity WB·identity CCM·프레임 정규화 없는 시각화와 실제 출력 배열이 동일함을 시험한다. Denoise·Sharpen·LSC·HDR Merge·Auto exposure/WB·시간 상태는 적용하지 않는다.

## 형상·CFA 위상과 입력 보존

- `native_sensor` 형상에는 `full_sensor_shape`와 `crop_origin=[row,column]`이 반드시 있어야 하며, 잘라낸 배열 전체가 센서 안에 들어가야 한다. 원래 센서의 CFA가 RGGB일 때 홀수 Crop 원점은 지역 CFA를 GRBG/GBRG/BGGR 위상으로 바꾼다. 이 위상을 보존하며 RAW 재배열·보간·Resizing은 하지 않는다.
- 기존 정규화 상고 Raster는 `kind=representative_normalized_field_raster`, 정확한 `raster_shape`, `mapping=center_to_corner_radius_v1`을 사용한다. 이를 실제 연속 Native Sensor 영상으로 재표기하지 않는다.
- Crop 가장자리 밖의 원본 샘플은 사용할 수 없으므로 반사 경계 보간을 한다. Crop의 내부 결과는 같은 위치의 Full-frame 처리와 일치할 수 있지만, Crop 경계 결과까지 동일하다고 주장하지 않는다.
- 읽기 전용 입력과 Strided crop도 처리할 수 있다. 모든 출력은 새 배열이며 원본 RAW와 메모리를 공유하지 않는다. 처리 전후 C-order 배열 데이터 바이트 해시를 확인한다. 원본 컨테이너 바이트 해시와 배열 바이트 해시는 구분한다.

프레임 수는 1–64, 각 변은 2–4096의 짝수, 전체 `frames*rows*columns`는 1,048,576 이하이다. 중간 Float64 배열을 모두 보존하기 위한 한도이며 큰 Native 영상을 축소해 우회하지 않는다. 최대 저장 배열은 약 128 MiB이고 임시 작업 배열·라이브러리 메모리는 추가된다. 이는 배열 계산에 따른 예산으로, 실측 Peak memory나 처리량 보증은 아니다.

## 결과와 서비스 연결 경계

`process_raw()`는 `{summary, arrays}`를 반환한다. Summary는 전체 프로필과 해시, 입력 형상·Dtype·배열 바이트 해시, 정규화와 CFA 위상, 적용 단계, NumPy/SciPy/pyisetcam 버전 및 실행 소스 해시를 보존한다. Frame별 ADC 0/White 코드, Black 아래 표본 수와 Tone의 하단/상단 클리핑 채널 표본 수도 기록한다. ADC Endpoint가 없다는 사실은 숨겨진 Full-well clipping이나 센서 선형성을 증명하지 않는다.

| Array | 축 / 의미 |
| --- | --- |
| `black_subtracted_normalized_frames` | `[F,H,W]`, 부호를 보존한 정규화 BLC 값 |
| `demosaiced_rgb_frames` | `[F,H,W,3]`, 보간된 Sensor RGB |
| `white_balanced_rgb_frames` | `[F,H,W,3]`, 고정 WB 적용 후 |
| `ccm_rgb_frames` | `[F,H,W,3]`, Tone 이전의 부호·범위 보존 RGB |
| `rgb_frames` | `[F,H,W,3]`, Tone 적용 후 전체 프레임 |
| `rgb` | `[H,W,3]`, 기존 Preview를 위한 첫 프레임의 별도 사본 |

모든 배열 단위는 `1`이며 단위 없는 정규화 코드/채널 값이다. `rgb`는 평균 영상이나 대표 프레임을 선택하는 연산이 아니다.

이 순수 처리 함수는 취득 조건을 입력받아 수정하지 않는다. 서비스는 원본 Capture 또는 Camera Run, 불변 ISP 모델·프로필, 취득/프레임 순서·실제 적용 Exposure/Gain/Timing 조건, Source kind와 측정 계보를 그대로 묶어 Replay 작업으로 기록해야 한다. Replay 입력에 센서 교체·노출/Gain 변경·새 잡음 Seed를 허용하면 보존 RAW 재처리의 의미가 깨진다. 새 Scene Runtime은 자신의 센서 Readout으로 생성한 RAW에 동일 ISP 함수를 호출하고, 획득 경로의 구분을 상위 기록에 유지한다.

프로필 비교 출력은 구현 근거다. 훈련에 사용한 RAW는 Replay 후에도 훈련 자료이며 독립 물리 검증 자료로 바뀌지 않는다. 예측 불확실도는 `unknown`, 물리 검증은 `not_established`로 남는다. 장비 실측의 색오차·해상도·SFR이나 다른 상용 ISP와의 동등성을 인정하려면 별도의 명시적 관측·검증 계약이 필요하다.

## 구현 검증

[test_isp_replay.py](../tests/platform/test_isp_replay.py)는 네 Native Crop 위상의 상수 RGB 보존, 독립 Impulse 보간 가중치, WB/CCM 연산 순서·실제 출력 변화, Tone 전 음수/상단 범위 보존, 독립 sRGB 수식, 모든 프레임·입력 바이트·읽기 전용 배열 보존, 실제 기존 pyisetcam 출력과의 동등성, 잘못된 형상·취득 파라미터·프로필·Nonfinite·자원 초과 거부를 시험한다. 이 시험 Fixture는 합성 입력이며 물리 성능 검증 데이터가 아니다.

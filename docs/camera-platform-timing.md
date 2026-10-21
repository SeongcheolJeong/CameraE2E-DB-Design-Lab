# 기록된 시간·제어 계약

`camera_platform.timing`은 분광 영상 Timeline과 실제 적용 설정의 시간 관계를 검사하는
순수 모듈이다. 노출 구간을 계산하며 전자 회로·Readout·Transport·AE/AWB 제어기를
시뮬레이션하지 않는다. 명령값을 실제 적용값으로 바꾸거나 누락된 지연을 추정하지 않는다.
등록·출처·모델 유효 범위·ISP 프로필 해석은 이 계약을 사용하는 서비스의 책임이다.

## 공통 Clock

모든 시간 값은 정수 `ns`이며 다음 선언을 공유한다.

```json
{
  "clock_id": "capture-clock",
  "unit": "ns",
  "reference": "relative",
  "origin": "Declared capture trigger; absolute-clock calibration unknown"
}
```

`clock_id`와 `origin`을 포함한 네 필드는 두 Sequence에서 정확히 같아야 한다. 같은
숫자나 단위만으로 다른 Clock을 동기화하지 않는다. Clock의 진위·정확도·Drift는 이
선언으로 검증되지 않으며 시간 정확도는 `unknown`으로 남는다.

Timestamp의 범위는 `[-(2**53-1), 2**53-1]`이다. JSON/브라우저의 Binary64 정수 정밀도를
보존하려는 제한이며, 계산은 Python 정수로 수행한다. 행 Offset의 곱셈·끝 시각·전체
시간 Span도 범위를 검사한 뒤 반환한다. 단위를 초로 바꾸는 Runtime은 먼저 정수
시각의 차이를 구해야 한다. Absolute Epoch를 Float로 바꾸어 뺄셈하면 안 된다.
음수 상대 시각은 허용하며 소수·문자열·Boolean·NaN·Infinity는 시간으로 받지 않는다.

## TimedSpectralScene

공개 입력은 이미 등록된 분광 이상 영상의 ID를 참조한다.

```json
{
  "schema": "timed_spectral_scene_v1",
  "clock": {
    "clock_id": "capture-clock",
    "unit": "ns",
    "reference": "relative",
    "origin": "Declared capture trigger; absolute-clock calibration unknown"
  },
  "interpolation": "linear_irradiance",
  "knots": [
    {"time_ns": 0, "spectral_image_id": "spectral-image:first"},
    {"time_ns": 10000000, "spectral_image_id": "spectral-image:last"}
  ]
}
```

2–128개 Knot의 `time_ns`는 엄격히 증가해야 한다. 보간은
`linear_irradiance` 또는 `zero_order_hold`만 받는다. 연산 구간은 반개구간
`[start, end)`이며 마지막 Knot의 시각은 보장된 입력 범위의 끝이다. 마지막 값이
무한히 유지된다고 추정하지 않는다. 노출 끝이 마지막 Knot과 정확히 같은 경우는
허용하고 1 ns라도 넘으면 거부한다.

서비스가 ID를 확인해 실제 배열과 Metadata를 고정한 뒤에는 내부적으로 Knot을
`{"time_ns": ..., "scene": <기존 분광 영상 객체>}`로 확장할 수 있다.
`validate_timed_scene(..., expanded=True)`는 이 경계를 명시적으로 구분한다.
기본 Validator는 배열 대신 공개 ID만 받는다. Expanded 모드의 Scene 객체 내부
파장·광량 단위·Native 격자·효과·출처 검사는 공간 Runtime의 책임이며, Timing
Validator를 통과했다고 영상 데이터가 검증된 것으로 취급하지 않는다.

유한한 Knot의 광량 보간은 선언된 시간 입력의 근사다. 그 구간을 해석적으로
적분하더라도 실제 연속 움직임·광원 파형·Rolling shutter 정확도가 증명되는 것은 아니다.

## RecordedControlSequence

노출·Gain·ISP 모델은 각 프레임의 `applied`에서 반드시 제공한다.

```json
{
  "schema": "recorded_control_sequence_v1",
  "clock": {
    "clock_id": "capture-clock",
    "unit": "ns",
    "reference": "relative",
    "origin": "Declared capture trigger; absolute-clock calibration unknown"
  },
  "mode": {
    "shutter": "rolling",
    "line_time_ns": 10000,
    "hidden_top_rows": 2,
    "readout_basis": "full_sensor_rows"
  },
  "frames": [{
    "frame_id": "frame:0",
    "frame_start_ns": 0,
    "commanded": {
      "command_id": "command:0",
      "issued_at_ns": -1000000,
      "exposure_ns": 5000000,
      "gain": 2.0,
      "isp_model_id": "isp:requested"
    },
    "applied": {
      "applied_at_ns": -500000,
      "exposure_ns": 4000000,
      "gain": 1.5,
      "isp_model_id": "isp:actually-applied",
      "command_id": "command:0"
    }
  }]
}
```

1–16개 프레임을 받으며 프레임 ID는 고유하고 `frame_start_ns`는 엄격히 증가한다.
`exposure_ns`는 1–3,600,000,000,000, Gain은 유한한 `[1e-6, 1e6]` 범위다.
이 넓은 구현 상한은 특정 Sensor의 지원 범위가 아니다. 서비스는 각 프레임의
실제 적용값을 선택한 Model·Assembly의 더 좁은 유효 범위와 별도로 대조해야 한다.

`commanded`는 선택 사항이다. `applied.command_id`는 반드시 명시하며 대응 명령을
모르면 `null`이다. 명령 ID는 전체 Sequence에서 하나의 고유한 발행 기록을 가리킨다.
앞 프레임의 `commanded`에 들어 있는 명령이 뒤 프레임에 적용될 수 있다. 명령이
저장된 프레임의 배열 위치 대신 `issued_at_ns <= applied_at_ns`를 확인한다.
발행된 명령이 아직 적용되지 않은 기록도 보존할 수 있다.

요청한 Exposure/Gain/ISP와 적용값은 달라도 된다. Validator는 값을 Clamp하거나
차이를 자동 보정하지 않는다. 적용 시각은 프레임 순서에 따라 뒤로 갈 수 없으며,
동일 적용 시각이 서로 다른 설정이나 명령 ID를 선언하면 거부한다. 동일 이벤트의
설정이 후속 프레임에서도 유지되는 것은 허용한다. 기록값의 인과관계를 검사하는
것이며 하드웨어가 이 설정을 실제 적용했는지 인증하는 것은 아니다.

## Native 행별 노출과 프레임 경계

호출자는 기존 형식의 Native Geometry와 프레임 크기를 제공한다.

```python
geometry = {
    "kind": "native_sensor",
    "full_sensor_shape": [1080, 1920],
    "crop_origin": [100, 200],
}
frame_shape = [64, 64]
```

전체 Sensor 행 수와 Crop 시작 행을 혼동하지 않는다. 행 `r`의 노출은 다음과 같다.

```text
start[r] = frame_start_ns
         + (hidden_top_rows + crop_origin[0] + r) * line_time_ns
end[r]   = start[r] + applied.exposure_ns
```

`frame_start_ns`는 숨은 상단 행 Offset을 더하기 전의 선언된 프레임 기준 시각이다.
설정은 Crop의 첫 행보다 이른 **전체 Sensor의 첫 활성 행**, 즉
`frame_start_ns + hidden_top_rows * line_time_ns`까지 적용되어 있어야 한다.
Crop 바깥 행을 버렸다는 이유로 늦은 설정 적용을 허용하지 않는다.

Global shutter는 `line_time_ns=0`, `hidden_top_rows=0`이며 모든 행의 노출 구간이 같다.
Rolling shutter는 양의 Line time이 필요하다. `readout_basis`는 항상
`full_sensor_rows`이며 Crop 행을 0번 행으로 다시 시작하는 모드는 지원하지 않는다.
형상은 2–512의 짝수 행/열, 전체 프레임 Pixel 합은 1,048,576 이하이며 Crop은
원래 Sensor 내부에 있어야 한다. 대표 Raster·Resize는 이 시간 계약에서 받지 않는다.

같은 물리 Pixel의 연속 프레임 노출은 겹칠 수 없다. 끝과 다음 시작이 정확히 같은
경우는 허용한다. 서로 다른 행이 동시에 노출되는 정상적인 Rolling 동작은
허용하므로 “이전 프레임의 마지막 행이 끝나야 다음 첫 행이 시작한다”는 조건을
강제하지 않는다. Multibuffer/HDR의 같은 Pixel 중첩 모드는 현재 미지원이다.

Timeline Coverage는 실제 계산하는 Crop의 모든 행을 대상으로 한다. 보이지 않는
전체 Sensor의 분광 영상을 만들어 Coverage를 넓히지 않는다. 반면 설정 적용
Deadline과 다음 절의 전체 프레임 완료 이벤트는 전체 Sensor 행 기준이다.

## 관측된 완료 이벤트와 모르는 지연

프레임에 다음 세 필드를 선택적으로 넣을 수 있다. 각 값은 정수 또는 명시적 `null`이다.

| 필드 | 의미 |
| --- | --- |
| `sensor_readout_end_ns` | 관측된 전체 프레임 Sensor Readout 완료 시각 |
| `transport_end_ns` | 관측된 전체 프레임 전송 완료 시각 |
| `application_arrival_ns` | 관측된 전체 프레임 애플리케이션 도착 시각 |

알려진 값은 전체 Sensor의 마지막 행 노출 끝 이후여야 하며 위 순서대로 감소할
수 없다. 미확정 중간 이벤트는 건너뛰어 알려진 두 이벤트의 순서만 검사한다.
이들은 전체 프레임 완료 이벤트다. Crop 일부의 스트리밍 도착이나 패킷별 Pipeline
시각을 이 필드로 입력하면 안 된다. 그런 장치의 모델은 별도 계약이 필요하다.

`summarize_timing()`은 두 끝점이 모두 기록된 경우에만 다음 차이를 반환한다.

- `command_to_applied_ns`: 연결된 명령 발행에서 실제 적용까지의 차이.
- `readout_to_transport_end_ns`: Readout 완료에서 전송 완료까지의 차이.
- `transport_end_to_application_ns`: 전송 완료에서 애플리케이션 도착까지의 차이.

차이는 Hardware 성능의 인증값이 아니다. 특히 Readout 완료와 전송 완료의 차이를
전체 전송 시간으로 바꾸지 않는다. 전송 시작을 모를 수 있기 때문이다. 명령 자체의
전송·장치 도착 Timestamp는 이 계약에서 확보하지 않으므로
`command_transmission_ns`, `command_arrival_ns`는 `null`이다. 다른 미확정 완료
이벤트도 요약에서 `null`로 유지한다. RAW 생성 완료를 실제 Readout/Transport
완료 시각으로 사용하거나 Frame period에서 지연을 역산하지 않는다.

## 순수 Python 인터페이스와 검증

```python
from camera_platform.timing import (
    validate_timed_scene, validate_control_sequence, validate_timing_coverage,
    row_windows, summarize_timing,
)

scene = validate_timed_scene(public_timeline)
control = validate_control_sequence(recorded_control, geometry=geometry, frame_shape=frame_shape)
coverage = validate_timing_coverage(scene, control, geometry=geometry, frame_shape=frame_shape)
windows = row_windows(control["frames"][0], control["mode"], geometry, frame_shape)
diagnostics = summarize_timing(control, geometry=geometry, frame_shape=frame_shape)
```

정상 반환은 입력과 분리된 JSON Dictionary다. 반환값을 수정해도 원본 기록이 바뀌지
않는다. 거부는 `TimingError(ValueError)`와 구조화된 `issues`를 반환한다. 알 수 없는
필드나 누락값을 기본 설정으로 대체하지 않는다. 노출을 실제 광량으로 적분하고
전자·RAW·프레임별 ISP로 연결하는 작업은 이 모듈 밖의 Runtime 책임이다.

[Timing 시험](../tests/platform/test_timing.py)은 수작업으로 계산한 Crop/숨은 행 Offset,
Global/rolling 경계, 동일 Pixel 중첩, 전역 명령 연결과 요청/적용 차이, 미확정 지연,
1 ns Coverage 부족, Clock 불일치, JSON 정수 경계와 파생 Overflow를 검증한다.
이 시험은 구현 계약의 증거이며 실제 Sensor 타이밍·Timestamp 정확도·폐루프 제어
동작이나 물리 검증 PASS를 주장하지 않는다.

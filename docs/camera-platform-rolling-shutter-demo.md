# Global / Rolling Shutter · 번짐과 행별 기하 변형

실제 활성영역의 ROI 위치와 전체 센서 행 수·line time을 사용하는 후속 모드는
[센서 행 timing 데모](camera-platform-sensor-row-timing.md)를 참고한다.
이 문서와 기존 `shutter_motion` 결과의 가상 256행 정의는 변경하지 않는다.

DB Design Lab의 `shutter_motion` 모드는 같은 렌즈·센서·노출에서 global shutter와
rolling shutter의 native RAW·고정 ISP를 비교한다. 기존 조리개·광량·움직임 실행은
수정하지 않으며, 원본 `run-9692f54b37ab4c5d80f4e9edbd4e83dd`의 잡음 없는
R/Gr/Gb/B 응답을 사용해 행별 취득을 새로 계산한다.

## 무엇을 바꾸는가

- 기본 렌즈·센서: 보존 Canon p0248 F/4.12 × IMX462 구조 기반 모델.
- D65 장면 휘도 100·1000 cd/m², 수평 물체 속도 0·1·4m/s, 속도 환산 거리 20m.
- 노출 1·4·16ms; first-to-last row scan 0·5·15·30ms. 0은 global shutter다.
- 각 조건 12후보, 중심·outer-ne의 256×256 native 탐침, 4차트·8회 독립 취득.
- 동일 광학·픽셀·노출·CCM·WB·tone·표시 범위. 기존 회절·수차와 광량 차이를 유지한다.
- 다른 F-number는 기존 원본 탐색에 실제로 있는 값만 선택할 수 있다.

**readout_ms는 가상 256행 탐침의 첫 행과 마지막 행 사이의 스캔 시간이다.**
제품의 전체 센서 readout 또는 전송 지연이 아니다. 행 시간은 `readout_ms / 255`다.
실제 센서가 더 많은 행을 가지면 제품의 행 시간과 탐침 위치를 별도로 연결해야 한다.
30ms 스캔은 이 데모에서 약 117.65µs/행을 뜻한다. 이 정의는 화면과 결과에 함께 표시한다.

## 시간과 영상 형성

행 수 H=256, 행 r=0…255, 노출 T, 스캔 R에 대해 공통 기준은 센서 중앙(127.5행)의
노출 중앙 시각 t=0이다. 행 노출 중앙은 `(r/(H-1) - 0.5) * R`, 시작과 끝은 각각
`중앙 - T/2`, `중앙 + T/2`다. 총 취득 시간 범위는 R+T이며, 이를 전송·처리 지연과
같다고 해석하지 않는다. GS도 같은 기준 시각을 사용한다.

속도는 기존과 동일하게 `f / pitch * speed / distance`로 영상면 px/s로 환산한다.
초점거리 24.73288mm, pitch 2.9µm를 사용하며, 거리 입력으로 차트의 물체 크기·초점·PSF를
다시 만들지는 않는다. 고정 카메라 앞의 수평 등속 이동이고 위 행은 먼저, 아래 행은 나중에 취득한다.

전체 4채널의 잡음 없는 native 평균 전자율에 **행별 위치와 노출 구간을 한 번에 적분**하고,
고정 RGGB 사이트를 선택한 뒤 암전류·shot/read noise·well·ADC를 새로 계산한다.
이미 촬영한 RAW를 비틀거나 blur한 것이 아니다. 저장된 native 선형 보간 모델의 삼각 basis를
해석적으로 적분하며, blur 후 별도 subpixel 보간을 반복해 추가 흐림을 만들지 않는다.

- 행 안의 이동 길이 `velocity * T`는 노출 번짐이다.
- 첫 행과 마지막 행 사이의 이동 차이 `velocity * R`는 스캔에 따른 기하 변형이다.
- 스캔 0은 기존 global 적분 연산과 동일하다. 물체 속도 0이면 스캔 시간과 무관하게 같은 취득이다.
- 균일장 시간 SNR은 같은 노출에서 스캔 시간에 의해 증가하지 않는다. 전기 모델은 동일하며,
  스캔 속도별 회로 잡음·온도·대역폭 변화는 새로 모델링하지 않는다.

저장된 256×256 응답 밖의 장면은 없다. 가장 빠른 속도·가장 긴 노출/스캔의 지지 영역과
ISP 주변 영역을 제외한 동일 유효 사각형을 모든 후보에 사용한다. 패딩된 외곽은 화면에서
표시하고 지표에서 제외한다. 고정 중앙 edge ROI를 지지하지 못하는 요청은 명시적으로 실패한다.

## 번짐과 기하 변형을 별도로 평가

시간 SNR·신호·포화는 기존 반복 RAW 지표를 재사용한다. 처리 후 SFR은 고정 차트의
실제 RAW·ISP에서 계산한다. Rolling shutter는 edge 방향까지 바꾸므로, SFR만 높다고
global보다 좋다고 추천하지 않는다. 노출 선택은 **각 스캔 시간 안에서 따로** 수행한다.

기하 진단은 같은 조리개·노출의 잡음 없는 GS 응답과 RS 응답에서 행별 edge 위치를
측정하고 그 차이를 fitting한다. 관측한 행 범위의 이동 차이와 256행 전체로 외삽한 값을
구분하며, 이론적 `velocity * R`와 잔차를 함께 보존한다. 이 지표는 제품 실측 기하 오차나
잡음 있는 자연 영상의 optical flow가 아니다. 기준 응답·행 위치·잔차·해시를 추적할 수 있다.

## 실행

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=600) as client:
    job = client.submit_shutter_motion(
        "run-9692f54b37ab4c5d80f4e9edbd4e83dd",
        f_numbers=[4.12], luminance_cd_m2=[100, 1000],
        object_speed_m_s=[0, 1, 4], object_distance_m=20,
        exposure_ms=[1, 4, 16], readout_ms=[0, 5, 15, 30],
    )
    result = client.wait(job["id"], timeout=14400, poll_interval=10)
    assert result["status"] == "succeeded", result.get("error")
    print(result["result"]["summary"]["result_url"])
```

같은 경로의 CLI:

```sh
PYTHONPATH=src .venv-platform/bin/python tools/run_db_design_demo.py \
  --comparison shutter_motion --source-run run-9692f54b37ab4c5d80f4e9edbd4e83dd \
  --f-numbers 4.12 --luminance-cd-m2 100 1000 --object-speed-m-s 0 1 4 \
  --object-distance-m 20 --exposure-ms 1 4 16 --readout-ms 0 5 15 30 \
  --output outputs/db-design-shutter-20261017
```

공통 POST `/api/v1/db-design/studies`와 기존 조회·대기·취소·파일·bundle을 사용한다.
양수 스캔과 짝이 되는 GS(0ms)가 항상 필요하다. 요청은 최대 216후보의 명시적인 실행 예산과
SSD 500GB 프로젝트 예산, 실제 입력 지지 범위 검사를 받는다. 자동 해상도 축소는 없다.

원본 RAW·평균 응답·행별 시각·적분 커널·calibration·원본 revision/해시·기하 지표를 보존한다.
화면 후보 선택은 저장된 결과 탐색이며, 새로운 속도·노출·스캔은 제출 후 계산이 완료되어야
결과로 표시한다. rolling-shutter 보정·3D 가림·독립 물체/배경·수직 이동·제품 고유 mode는
이번 범위에 포함하지 않는다.

## 준비된 실행 · 2026-10-17

`run-7cb31e7ba05f40b0afc000312baeabfb`에 위 기본 설정의 실제 계산을 보존했다.

- 6개 광량·속도 조건 × 12개 노출·스캔 후보 = 72조합.
- 2개 시야 × 4차트 × 8회로 정수 RAW 4,608장. 패키지 10,317,449,900 bytes.
- 엔진 실행 469.67초, 제출·용량 검사·해시 저장·SDK 대기 포함 647.92초.
  이 벽시계 시간은 카메라 readout/처리 지연이 아니다.
- 외장 위치: `/Volumes/T7/CameraE2E/reference-workshop/run-7cb31e7ba05f40b0afc000312baeabfb`.
- manifest SHA-256: `6403b3c48c1ba167f122051133928adf1a2c6cd77c6649090a97b16a3be94829`.

[기본 GS / RS 비교 열기](http://127.0.0.1:8780/db-design.html?run=run-7cb31e7ba05f40b0afc000312baeabfb&condition=condition-005&candidate-a=candidate-03-e01-r00&candidate-b=candidate-03-e01-r03&field=outer-ne&chart=resolution&stage=isp).

1000 cd/m², 4 m/s, 거리 환산 20m, F/4.12, 4ms, outer-ne의 결과:

| 256행 스캔 | 노출 이동 길이 | edge에서 관측한 39행 span 변위 | 전체 255행 span 외삽 | GS 대비 기울기 변화 |
| --- | --- | --- | --- | --- |
| GS 0ms | 6.823px | 0 | 0 | 0° |
| RS 5ms | 6.823px | 1.306px | 8.541px | 1.903° |
| RS 15ms | 6.823px | 3.915px | 25.596px | 5.657° |
| RS 30ms | 6.823px | 7.828px | 51.181px | 11.122° |

같은 노출에서 균일장 SNR은 모두 26.818dB다. 스캔 시간은 기하 변형을 바꾸고,
이 고정 전기 모델의 균일장 잡음은 바꾸지 않는다. 기본 25dB 조건에서 100cd/m²는
모든 속도·스캔에 적합 후보가 없고, 1000cd/m²는 각 스캔 그룹에서 4ms를 선택한다.
속도 1m/s의 일부 RS SFR이 GS보다 조금 높은 것도 전체 화질 우위로 표시하지 않는다.

검증 기록은 `outputs/db-design-shutter-20261017/`에 있다. `verification.json`은
23,410개 파일 해시, 4,608개 정수 RAW, 144개 시야 지표/기하 결과, 24개 그룹 선택을
검사했다. 이전 GS·정지 입력·균일장 비교 3,168쌍의 RAW는 정확히 같았으며,
독립 시간 적분 8건의 상대 오차는 최대 3.02e-9, RAW/ISP 재실행 24건도 일치했다.
이는 구현·모델 내부 검사이며 제품 실측 검증은 아니다.

`ui-verification.json`은 실제 72후보의 파일 연결과 스캔별 선택을 확인한다.
계산 완료 화면은 자산 목록 갱신을 기다리지 않고 결과부터 연다. 매우 가까운 소수
스캔 값의 그룹 키가 합쳐지던 경계 오류도 수정했으며, 저장된 기본 6조건의 모든
선택 결과는 수정 전후 동일하다(`selection-key-regression.json`).

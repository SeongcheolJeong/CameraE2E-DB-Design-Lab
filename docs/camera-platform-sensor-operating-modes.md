# IMX462 operating-mode timing · FPS와 노출 제약

DB Design Lab의 전체 행 시간축에 모드별 line time·VMAX·프레임 주기와
노출 범위를 연결한다. 256×256 native ROI 위치와 광학 anchor는
[센서 행 시각](camera-platform-sensor-row-timing.md)의 공통 좌표를 사용한다.

## 확보한 근거

기존 TechInsights `DEF-2011-804` revision에는 `sensor_readout_modes` 행이 없었다.
Sony의 공개 flyer는 1080p·720p 최대 120fps와 1H 단위 shutter를 공개하지만
모드별 HMAX/VMAX/clock 조합을 제공하지 않는다. 최대 FPS로 line time을 확정하지 않는다.

추가로 확보한 [Kurokesu 제조사 드라이버](https://github.com/Kurokesu/imx462-jetson-driver/tree/59bdf67a21b67572039bd31d8c4e71f66dd2c441)를
고정 revision과 SHA-256으로 보존했다. 코드를 실행하거나 simulator에 복제하지 않고
register·clock·control의 수치를 독립적으로 추출했다.

| 근거 | 확보한 값 |
| --- | --- |
| `imx462_mode_tbls.h` | 1920×1080, 2lane, RAW10, HMAX 4400 |
| device-tree `mode0` | timing clock 148.5MHz, FPS 2–30, 요청 노출 30–33274µs |
| `nv_imx462.c` | 최소 VMAX1125, FPS에 따른 VMAX 계산, 행 단위 노출 반올림·SHS1 계산 |

이 값은 센서 제조사의 비공개 register datasheet나 실측값과 동일한 상태로 표시하지 않는다.
모듈 제조사 드라이버에서 확보한 설정과 그 설정으로부터 계산한 값이다.

## 프로필별 상태

| 프로필 | Line time | 프레임 주기 | 근거·제약 |
| --- | --- | --- | --- |
| `kurokesu_1080p30_raw10` @30fps | 29.629630µs | 33.333333ms | 확보한 HMAX/clock에서 계산; VMAX1125 |
| 같은 프로필 @15fps | 29.629630µs | 66.666667ms | 드라이버 FPS 식으로 VMAX2250; 노출 control 상한 33.274ms 유지 |
| `assumed_1080p60` | 14.814815µs | 16.666667ms | HMAX2200·clock148.5MHz·VMAX1125는 가정 |
| `assumed_1080p120` | 7.407407µs | 8.333333ms | HMAX1100·clock148.5MHz·VMAX1125는 가정 |

60·120fps의 line time, lane/ADC 조합, exposure guard는 미확보다.
Sony가 120fps를 지원한다고 명시한 사실만으로 위 가정을 실제 register mode로 승격하지 않는다.
720p120은 window 위치·행 매핑·timing이 미확보이므로 기존 full-active ROI로 실행하지 않는다.

원본 근거는 `camerae2e_db/sources/sensor_timing/imx462/kurokesu/`,
추출한 상태는 `operating-mode-evidence.v1.json`에 있다. 원래 구조 DB revision은 유지한다.

## 실제 취득에 적용하는 제한

```text
line time = HMAX / timing clock
VMAX = max(1125, floor(clock / HMAX / requested FPS))
frame period = VMAX × line time
coarse exposure = round(requested exposure / line time)
SHS1 = VMAX − coarse exposure − 1
```

30fps 모드의 요청 1ms는 34행, 실제 1.007407ms가 된다.
GS 비교군과 RS는 같은 실제 노출로 계산한다. 4ms·16ms는 이 프로필에서
정확히 행 단위에 맞는다. 120fps 가정 프로필의 16ms 요청은 차단하며 RAW를 생성하지 않는다.
노출을 맞추려고 FPS를 자동으로 낮추거나 frame period를 늘리지 않는다.

반대로 rolling shutter에서 scan span과 노출을 단순히 더해 frame budget으로
검사하지 않는다. 행별 노출은 겹칠 수 있으며 같은 행의 반복 주기를 제한한다.
프레임 주기·다음 frame 예정 시각은 row-timing artifact에 별도로 저장한다.
8회 잡음 반복은 독립 취득이며 시간적으로 움직이는 8프레임 시퀀스로 주장하지 않는다.

구조 모델의 readout은 12bit·well10000e⁻·read noise10e⁻ 등의 기존 가정을 유지한다.
드라이버 RAW10 설정을 readout 실측 calibration으로 옮기지 않는다.
화면은 timing 근거의 bit depth와 실제 계산 readout을 함께 표시한다.
Active→total offset·ROI 배치·CFA phase·scan 방향·정확한 모듈 variant 일치도
기존 가정이며, CSI/ISP 전송 지연은 미확보다.

## SDK와 재사용

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=600) as client:
    job = client.submit_sensor_shutter(
        "run-9692f54b37ab4c5d80f4e9edbd4e83dd",
        operating_mode_id="kurokesu_1080p30_raw10", fps=30,
        located_source_run_id="run-56c734b5276e4d03b33df239ae61a032",
        f_numbers=[4.12], luminance_cd_m2=[100,1000],
        object_speed_m_s=[0,4], object_distance_m=20,
        exposure_ms=[1,4,16],
    )
    result = client.wait(job["id"], timeout=14400)
    assert result["status"] == "succeeded", result.get("error")
    print(result["result"]["summary"]["result_url"])
```

모드 지정 시 `line_time_us`의 임의 override는 거부한다.
모드 없이 직접 설정한 기존 요청은 가정값의 의미를 유지한다.

재사용은 동일한 렌즈·센서·calibration·ROI profile과 manifest 해시가 일치하는
PSF와 **잡음 전** 전자율에 한정한다. 저장된 noisy/clipped RAW를 배율 조정하지 않는다.
기존 준비 RAW를 복사해 보존할 때는 신규 취득 수에 넣지 않는다.
다른 준비 모드에서도 같은 요청 축을 사용하면 보수적인 공통 유효영역을 적용한다.
500GB SSD 예산, 취소·실패·조회·대기·bundle 경로도 기존 계약을 사용한다.

새 결과에는 `operating-mode.json`, `exposure-schedule.json`,
`located-source-reuse.json`, 시야별 `row-timing.json`과 원본 RAW·ISP를 보존한다.

## 준비된 실제 실행 (2026-10-17)

| Timing 프로필 | 완료 결과 | 신규 RAW |
| --- | --- | --- |
| 30fps · 제조사 드라이버 근거 | [비교 데모](http://127.0.0.1:8780/db-design.html?run=run-97eea81b38e24e7bbdb17f67ce0da8b5&condition=condition-003&field=outer-ne&chart=resolution&stage=isp) | 1536 |
| 60fps · line time 가정 | [비교 데모](http://127.0.0.1:8780/db-design.html?run=run-df7325de276844339a7222424da13eaa&condition=condition-003&field=outer-ne&chart=resolution&stage=isp) | 1536 |
| 120fps · line time 가정 | [비교 데모](http://127.0.0.1:8780/db-design.html?run=run-53dd407caef34581845b3fa909a35e79&condition=condition-003&field=outer-ne&chart=resolution&stage=isp) | 1024 |

휘도 100·1000cd/m², 속도 0·4m/s, F/4.12, 거리 가정 20m,
요청 노출 1·4·16ms를 사용했다. 각 모드에서 같은 실제 노출의 GS/RS를 비교한다.
120fps 가정 프로필의 16ms는 차단되어 해당 후보·RAW가 없다.
모든 준비 결과의 공통 유효영역은 `[8,248,48,208]` (끝 제외)이다.

신규 RAW 총 4096장, 64개 카메라/노출 조건, 패키지 총 10,061,790,442bytes를 보존했다.
각 패키지의 기존 준비 RAW64장 사본은 신규 취득 수에 포함하지 않는다.
기존 PSF·광전자율·calibration 해시를 확인했으며 이전 결과를 덮어쓰지 않았다.

30fps·4m/s·4ms의 실제 영상 경계 기준 이동은 중심 −0.02531px,
NE −18.01756px였다. 두 ROI 중심의 취득 시각은 각각 −0.014815ms·−10.562963ms다.
관측된 경계 기울기를 ROI 255행 간격으로 외삽한 shear는 12.88927px·12.89165px였다.
전체 센서 프레임의 실측 왜곡으로 해석하지 않는다.

선택된 회귀 83개, source/assumption·FPS·노출 API 거부 검사, 세 모드의 전체 artifact 해시,
RAW/ISP24건 재현, 6건의 독립 시간 적분, 실제 HTTP 파일 다운로드가 통과했다.
독립 적분의 최대 상대차는 6.18×10⁻⁹다. 일부 움직임/시야에서 SFR 추정이
불가능한 경우는 무효로 유지한다. 이를 특정 FPS의 물리적 우열로 해석하지 않는다.

재실행은 `outputs/db-design-sensor-modes-20261017/run_modes.py`이며,
`deliveries.json`, `verification.json`, `http-verification.json`과 브라우저 증거를 함께 보존한다.
비교 그림은 `mode-timing-comparison.png`다. 각 실행의 원본 파일은 외장 SSD의
`/Volumes/T7/CameraE2E/reference-workshop/<run-id>/`에 있다.

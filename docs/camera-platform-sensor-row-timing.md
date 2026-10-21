# 실제 센서 위치의 ROI · line time 비교

현재는 [모드별 timing·FPS·노출 제한](camera-platform-sensor-operating-modes.md)도 지원한다.
1080p30 모듈 드라이버 근거와 미확보 high-FPS 가정을 구분한다. 아래의 10·20µs
설명과 준비 결과는 기존 직접 설정 실행의 의미이며, 새 근거로 덮어쓰지 않는다.

DB Design Lab의 `sensor_shutter`는 활성 센서 안의 중심·주변부 ROI를 하나의
행별 취득 시간축에 배치한다. `shutter_motion`의 가상 256행 scan 정의와 기존
저장 결과는 그대로 유지한다. 새 작업은 `line_time_us`를 입력으로 받는다.

## 확보한 값과 가정값

기존 TechInsights 구조 모델 `DEF-2011-804`의 고정 revision은
`0ccf0fae-faa3-5001-88d5-0e30d37bfec6`이다. 해당 DB에는 pixel pitch 2.9µm가
있지만 width/height와 line time은 없었다. 이 원본 revision을 덮어쓰지 않는다.

추가 확보한 [Sony IMX462 family flyer](https://www.sony-semicon.com/files/62/pdf/p-12_IMX462LQR_LQR1_LLR_Flyer.pdf)의
IMX462LQR Ver2.0 표(pages 1–2)는 다음을 구분한다.

| 영역 | 가로 × 세로 | 출처 상태 |
| --- | --- | --- |
| 전체 픽셀 | 1945 × 1109 | 새로 확보한 공식 자료 |
| 유효 픽셀 | 1945 × 1097 | 새로 확보한 공식 자료 |
| 활성 픽셀 | 1937 × 1097 | 새로 확보한 공식 자료 |
| 기록 영상 | 1920 × 1080 | 새로 확보한 공식 자료 |

센서 구조 DB의 정확한 LQR 세부형 일치는 확인되지 않았다. 공식 자료의 최대 FPS로
line time을 역산하지 않는다. 실제 HMAX/VMAX·mode register·blanking·scan order·
활성영역의 total 좌표 offset·CFA phase와 실측 timing은 아직 미확보다.

데모 line time은 0·10·20µs이며 **사용자 변경 가능한 가정값**이다. 0은 같은
입력의 가상 global shutter 기준이며 IMX462가 global shutter 제품이라는 뜻이 아니다.
위→아래 순서, 활성영역의 total origin [12,4], 광축 [548,968], RGGB phase도 가정이다.
전체와 활성 영역의 차이가 12행이라는 사실만으로 그 12행이 모두 앞에 있다고
확정할 수 없다. total 좌표·전체 scan은 이 배치 가정에 조건부인 값으로 표시한다.

원본 PDF·URL·수집 기록의 SHA-256과 구조 revision hash를 timing profile에 고정한다.
PDF는 로컬 출처 증거로 보존하며 배포 패키지에는 사실·출처·해시를 기록한다.

## 실제 위치와 공통 시각

기존 `outer-ne` 광학 탐침은 이 센서의 활성 면적 밖이었다. 새 모드에서는 기존
주변부 응답에 행 번호만 붙이지 않는다. 같은 렌즈 처방·aperture와 센서 구조를
유지하고 아래 위치의 chief ray·회절 PSF·픽셀 응답을 새로 계산한다.

| ROI | 활성 좌표 시작 [행,열] | 중심 [행,열] | 영상면 중심 x,y (mm) |
| --- | --- | --- | --- |
| 중심 | [420,840] | [547.5,967.5] | [-0.00145,+0.00145] |
| 주변 NE | [64,1616] | [191.5,1743.5] | [2.24895,1.03385] |

위 표의 영상면 좌표는 ROI의 **기하학적 중심**이다. 국소적으로 한 PSF를 적용하는
기존 모델에서 중심 ROI의 광학 기준점은 광축에 놓인 실제 active [548,968],
local [128,128] 픽셀, 영상면 [0,0]mm로 따로 지정한다. 주변 ROI의 광학 기준점은
그 ROI 기하 중심과 같다. `roi_center_image_xy_mm`와 `optical_anchor_*`를 분리해
보존하며, 광학 기준점 선택으로 ROI의 행 시각을 이동하지 않는다.

각 ROI는 native 256×256이고 64px 광학 주변 입력도 활성영역 안에 들어온다.
짝수 crop origin은 가정한 RGGB phase를 보존한다. 기준 시각 t=0은 **활성 548행의
노출 중앙**이다. 각 ROI의 중앙을 따로 t=0으로 이동하지 않는다.

```text
행 노출 중앙 = (활성 행 번호 − 548) × line_time
행 노출 시작/끝 = 중앙 ∓ exposure/2
local ROI span = 255 × line_time
active span = 1096 × line_time
modeled total span = 1108 × line_time
```

20µs일 때 중심 ROI 중앙은 −0.01ms, NE 중앙은 −7.13ms다. 중심·NE 시간차는
7.12ms이고 각 ROI 내부 span은 5.10ms다. active span은 21.92ms,
가정한 전체 span은 22.16ms다. 전송 지연이나 실제 제품 frame period로 해석하지 않는다.

4채널 평균 전자율을 이 절대 행별 노출 구간에서 적분한 다음 CFA·shot/read noise·
well·ADC를 계산한다. RAW를 촬영한 뒤 비틀지 않는다. 동일 노출 GS와 비교한
edge 위치 차이에서 **ROI 전체 위치 이동과 행간 기울기**를 각각 측정한다.
전체 행 수로 외삽한 변위는 전체 프레임 실측과 구분한다. 기존 SNR·포화·처리 후 SFR도
보존하지만 line time 그룹 사이의 화질 우열을 SFR 하나로 결정하지 않는다.

## 실행

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=600) as client:
    job = client.submit_sensor_shutter(
        "run-9692f54b37ab4c5d80f4e9edbd4e83dd",
        f_numbers=[4.12], luminance_cd_m2=[100, 1000],
        object_speed_m_s=[0, 1, 4], object_distance_m=20,
        exposure_ms=[1, 4, 16], line_time_us=[0, 10, 20],
    )
    result = client.wait(job["id"], timeout=14400, poll_interval=10)
    assert result["status"] == "succeeded", result.get("error")
    print(result["result"]["summary"]["result_url"])
```

같은 POST `/api/v1/db-design/studies`, 조회·대기·취소·bundle을 재사용한다.
CLI는 `tools/run_db_design_demo.py --comparison sensor_shutter --source-run ...
--line-time-us 0 10 20`이다. `readout_ms`나 임의 센서 행 수를 새 요청에 섞으면
거부한다. timing profile·출처 해시·ROI 좌표를 제출 때 고정하고 실행 중에도 검사한다.

기본 예산은 54조합과 신규 위치 광학 계산이다. 500GB SSD 예산·현재 여유 공간·
입력 지지 범위 검사를 유지하며 해상도나 정확도를 자동으로 낮추지 않는다.

새 위치의 reciprocal pupil 적분은 공개 Factory의 기존 샘플 수에서 수렴을
통과하지 못했다. 기존 DB의 촘촘한 reciprocal adapter를 사용하며, 광학 기준점·
quadrature 수준·수렴 결과를 새 광학 revision에 기록한다. 실패한 최초 실행도 보존한다.

이는 고정 카메라, 수평 등속 차트, 국소 광학 응답을 이용한 모델 데모다.
실제 제품 mode·수직 이동·3D 가림·센서 회로의 line-time별 잡음 변화·실측 정합은
이 결과에서 검증하지 않는다.

## 준비된 실제 계산 결과 (2026-10-17)

[중심·주변부 global / rolling 비교 열기](http://127.0.0.1:8780/db-design.html?run=run-56c734b5276e4d03b33df239ae61a032&condition=condition-005&candidate-a=candidate-03-e01-l00&candidate-b=candidate-03-e01-l02&field=outer-ne&chart=resolution&stage=isp)

- F/4.12, 100·1000cd/m², 0·1·4m/s, 거리 가정 20m, 1·4·16ms,
  0·10·20µs/line의 54조합을 실제 계산했다.
- 비교 RAW 3456장과 새 위치 광학 준비용 RAW 64장을 보존했다.
  각 RAW는 native 256×256이며 전체 프레임을 촬영한 결과로 표시하지 않는다.
- 패키지 8,029,678,778bytes, SDK 제출부터 완료까지 1548.36초였다.
  이 시간은 로컬 시뮬레이션 실행 시간이며 카메라의 지연이 아니다.
- 결과 manifest SHA-256:
  `5c6c196ef44df5634f568fd22e49c7b3e0759a659883bac43d9d0966ded11482`.

1000cd/m²·4m/s·4ms·20µs/line에서 같은 노출 GS와 비교한 실제 평균 전자율
edge의 위치 이동은 중심 −0.01695px, 주변 NE −12.16182px였다.
관측한 39행 구간의 edge 기울기를 ROI의 255행 간격으로 외삽하면
각각 8.70391px·8.70741px다. 이 값은 전체 센서 프레임의 측정 왜곡이 아니다.
큰 왜곡에서 처리 후 SFR 지표가 무효인 후보는 통과나 권장으로 처리하지 않는다.

재실행 진입점과 원본·화면 검증 기록은
`outputs/db-design-sensor-shutter-20261017/`에 보존했다.
실행 파일은 `run_sensor_shutter.py`, 완료 기록은 `delivery.json`,
비교 그림은 `sensor-row-comparison.png`다. 실제 계산 자료는
`/Volumes/T7/CameraE2E/reference-workshop/run-56c734b5276e4d03b33df239ae61a032/`
에 있으며 이전 aperture / 가상 256행 결과를 덮어쓰지 않았다.

연결 회귀 110개, 결과 18107파일 및 원본 source 해시, 108개 ROI timing·기하·지표,
RAW/ISP 24건의 재실행과 실제 HTTP RAW 다운로드 동일성 검사가 통과했다.
독립 시간적분 8건의 최대 상대차는 3.02×10⁻⁹였다. 기록은 `verification.json`,
`package-verification.json`, `ui-verification.json`, `browser-verification.json`이다.
이 검사는 구현과 저장 결과의 일관성 확인이며 실제 센서 timing 검증은 아니다.

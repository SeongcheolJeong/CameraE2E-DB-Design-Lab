# Reference Sensor Mode Lab

같은 특허 렌즈와 IMX462 구조 기반 Reference에서 **취득·합산·readout 방식이 해상도, 저조도 잡음, 포화에 주는 변화**를 비교한다. 두 recipe는 Binning과 Dual Gain이다. 기존 Pixel Lab의 CFA·입사각 응답과 연결하지만, 이 readout을 실제 IMX462 제품 mode라고 표시하지 않는다.

추가된 [Readout Design Lab](camera-platform-reference-readout-lab.md)은 같은 화면에서
gain 배분, CDS·열 잡음, ADC 비트 수·포화를 비교한다. 기존 두 recipe의 취득·합성 의미는 유지한다.

## 고정 입력과 계산 범위

서버의 `GET /api/v1/reference-workshop/sensor-mode-studies`는 현재 template과 준비된 작업 목록을 반환한다. 최초 template은 chirp까지 포함한 `run-d2105e38b3cd4f83b3550ebff21ca7fd`와 manifest SHA-256 `5561e0c26de907f32b23772c864bfbab80c8c1b41f229867c113d25574d7926c`를 고정한다.

기준 조건은 D65·1000 cd/m²·4 ms·gain 1·25°C, RGGB, 독립 취득 8회다. 광량은 기준의 0.01·0.1·1배다. 중심과 `outer-ne`를 기본으로 하며, 같은 실행기는 저장된 9개 시야를 지원한다. 시야마다 실제 native 256×256 탐침을 사용한다. 전체 제품 센서 프레임이나 도로 시퀀스는 아니다.

- `uniform`, `resolution`, `texture`, `color`, `demosaic_chirp`는 보존 평균 전자율을 재사용한다.
- 1배 광량에서 source 취득 seed 조건이 같으면 보존 native RAW를 재사용한다. Digital binning은 항상 해당 native RAW에서 계산한다.
- 낮은 광량이나 변경 seed는 평균 전자율에서 새 shot/read noise·포화·ADC를 계산한다. 잡음과 clipping이 적용된 RAW를 배율 조정하지 않는다.
- `dynamic_range`는 같은 렌즈·픽셀 응답으로 새로 계산하는 16단계 분광 장면이다. 중심 Gr의 기대 신호가 기준 full-well의 0.001–2배를 덮도록 장면 광량을 사전에 정의한다. 모든 site와 시야의 실제 전자는 다를 수 있다.
- 일반 차트가 source에 없으면 먼저 해당 차트의 신규 취득이 필요하다. 재처리 안에서 누락된 chirp 등을 가짜로 생성하지 않는다.

## 두 가지 준비 데모

### Binning: 합산 위치와 해상도 비용

| 모드 | 계산·보존 의미 |
| --- | --- |
| Native | 256×256 정수 RAW와 signed 전자 추정 |
| Digital binning | Native RAW에서 black·예상 dark를 제거하고 같은 CFA site 4개를 합산한 128×128 파생 전자 배열 |
| 이상적 전하 binning | Native PRNU·dark·shot charge·pixel full-well 제한 후 같은 site 합산, 합산 node 제한, read noise 1회, ADC |
| 표시 보간 | Binned 결과의 nearest/bilinear 확대. 취득한 공간 정보는 늘지 않음 |

전하 binning의 기본 가정은 node capacity 4배, conversion gain ¼, read-noise RMS 동일이다. 설정은 명세의 `charge_binning`에 저장한다. Digital 합산은 새 sensor ADC RAW가 아니므로 파생 전자 배열로 구분한다. RAW와 합산 전자는 각각 손실 없이 보존한다.

**일반 RGGB의 동일 site 4개 합산은 균일한 half-resolution Bayer 격자가 아니다.** 물리적 중심은 native pitch 기준 `1,2,5,6,…`으로 놓인다. 계산은 R·Gr·Gb·B 각각의 실제 중심을 저장하고, `0.5+2i` 공통 출력 좌표에 bilinear 보간한다. 임의로 일정한 2-pitch Bayer로 취급하지 않는다. 이 제한을 가진 연구 모델이며 제조사의 특정 binning 배선·mode를 복제한 것이 아니다.

### Dual Gain: 동일 전하의 HCG·LCG·DCG

기존 공유 전하 readout과 RAW-only 합성을 재사용한다. 같은 Poisson 전하를 두 경로가 읽으므로 shot noise는 공유한다. 별도 노출 HDR처럼 독립 shot sample 두 개를 합치지 않는다.

- LCG는 기준 transfer를 유지한다.
- HCG는 conversion gain 4배, 입력 선형 한계 ¼, read-noise RMS ½로 선언한다.
- 경로 간 read-noise 공분산 0은 가정값이다. 공유 shot covariance는 유지한다.
- 합성은 경로별 RAW 경계 guard와 선언된 공분산을 사용한다. 포화·무효 경로를 제외하며 양쪽이 무효면 NaN과 무효 mask를 남긴다.
- HCG·LCG 정수 RAW, 합성 전자, 분산 추정, 유효 영역, 가중치·경로 일치 진단을 저장한다.

`reprocess_of`에 완료된 Dual Gain study를 지정하면 보존된 RAW 쌍과 calibration으로 다시 합성한다. 합성·표시 설정만 바꿀 수 있으며 취득 조건은 고정한다. 모델, 장면, 노출이나 seed를 바꾸는 것은 새로운 취득 작업이다.

## 동일 ISP와 지표 해석

모든 모드에 고정된 baseline 전자→RGB 행렬·LSC·Bilinear 처리·tone·display white를 적용한다. NR·sharpen은 OFF다. Binned 전자는 기여 픽셀 4개로 나누어 동일 native 면적 기준으로 표시한다. 프레임별 밝기나 최대값을 맞추지 않는다.

준비 실행기의 Binning display white는 1, Dual Gain은 16으로 고정한다. Dual Gain의 HCG·LCG·DCG 세 영상 모두 16을 사용하므로 전자적으로 유효한 밝은 단계가 표시 단계에서 일괄 clipping되는 것을 피한다. 프레임·mode별 자동 정규화가 아니다. 재합성은 원본 study의 display white를 그대로 보존한다.

Binned LSC는 native uniform 응답을 합산한 뒤 역수를 취한다. native gain의 단순 평균이 아니다. LSC는 한 번만 적용하며 분산은 `gain² / contribution_count²`로 변환해 저장한다. 이 mosaic 분산을 완전한 demosaic·CCM 출력 공분산으로 부르지 않는다. 처리 후 잡음은 실제 8개 반복 영상의 픽셀별 시간 분산에서 구한다.

화면과 보고서는 평균 전자, 시간 잡음, SNR, well·ADC 포화, 국소 대비, 처리 후 SFR을 연결한다. 사진·차트의 공간 표준편차를 시간 잡음으로 사용하지 않는다. 원본 RAW만 재사용한 경우 실제 clipping truth를 알 수 없으므로 RAW 경계 proxy임을 구분한다.

HCG·LCG의 well/path 포화에는 공유 전하 well 제한과 해당 readout 경로의 입력 선형 한계가 포함된다. 따라서 HCG의 ¼-full-well 경로 clipping을 물리적인 공유 well의 포화로 해석하지 않는다. DCG의 well 포화는 공유 전하 well 제한이며 ADC 포화와도 구분한다.

SFR은 모드별 native 출력에서 측정하고 `cycles/native pixel`과 해당 출력의 Nyquist를 표시한다. Binned Nyquist는 native 기준 0.25 cycles/pixel이다. 물체각 주파수는 해당 local probe의 선언된 FOV를 사용할 뿐, 작은 탐침을 전체 카메라 화각으로 해석하지 않는다. 처리 후 SFR은 광학 단독 MTF가 아니다. 유효 영역이 부족하거나 측정이 성립하지 않으면 이유와 함께 unavailable로 둔다.

화면의 native 크기와 공통 표시 크기 전환은 해상도 비용을 보여주기 위한 것이다. nearest/bilinear 선택은 저장된 결과의 표시 보간이며 재취득·재측정을 수행한 것으로 표시하지 않는다.

## API·SDK·한 번의 실행

| 용도 | API |
| --- | --- |
| template·목록 | `GET /api/v1/reference-workshop/sensor-mode-studies` |
| 제출·재합성 | `POST /api/v1/reference-workshop/sensor-mode-studies` |
| 작업 | `GET /api/v1/reference-workshop/sensor-mode-studies/{id}` |
| case | `GET /api/v1/reference-workshop/sensor-mode-studies/{id}/cases/{case_id}` |
| 파일 | `GET /api/v1/reference-workshop/sensor-mode-studies/{id}/files/{path}` |
| 묶음 | `GET /api/v1/reference-workshop/sensor-mode-studies/{id}/bundle.zip` |
| 비교 profile 저장 | `POST /api/v1/reference-workshop/sensor-mode-studies/{id}/profiles` |
| 저장 profile 조회 | `GET /api/v1/reference-workshop/sensor-mode-studies/{id}/profiles/{profile_id}` |

작업은 기존 `reference_workshop` Registry·Worker lifecycle을 공유한다. 대기는 `wait_reference_workshop()`, 취소는 `cancel_reference_workshop()`를 사용한다. 실패·취소·timeout을 완료로 처리하지 않는다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=120) as client:
    spec = client.reference_sensor_mode_studies()["template"]
    spec["recipe"] = "dual_gain"
    spec["charts"].append("dynamic_range")
    spec["isp"]["display_white_level"] = 16.0
    job = client.submit_reference_sensor_mode_study(spec)
    done = client.wait_reference_workshop(job["id"], timeout=7260)
    if done["status"] != "succeeded":
        raise RuntimeError(done.get("error") or done["status"])
    case = client.reference_sensor_mode_case(job["id"], "case-000")
    profile = client.save_reference_sensor_mode_profile(job["id"], {
        "name": "LCG versus DCG",
        "case_id": case["id"],
        "mode": "dcg",
        "baseline_mode": "lcg",
        "interpolation": "nearest",
    })
```

통합 서버가 이미 실행 중이면 그대로 사용한다. 실행 중이지 않을 때 저장된 프로젝트 상태로 다음과 같이 연다.

```bash
PYTHONPATH=src .venv-platform/bin/python -m camera_platform serve \
  --workspace /Users/seongcheoljeong/Documents/CameraE2E \
  --state-dir /Users/seongcheoljeong/Documents/CameraE2E/outputs/camera-platform-virtual-lab-cra-20261011 \
  --port 8780 --empty
```

SDK 준비 실행기는 서버 template을 받아 Binning, Dual Gain을 순차 제출하고 기다린다. 기본 출력 폴더는 `outputs/reference-sensor-mode-lab-delivery`다.

```bash
PYTHONPATH=src .venv-platform/bin/python tools/run_reference_sensor_mode_lab.py --recipe both
```

하나만 실행하거나 기존 쌍을 다른 guard로 재합성할 수 있다.

```bash
PYTHONPATH=src .venv-platform/bin/python tools/run_reference_sensor_mode_lab.py \
  --recipe dual_gain --output outputs/reference-sensor-dcg

PYTHONPATH=src .venv-platform/bin/python tools/run_reference_sensor_mode_lab.py \
  --reprocess run-REPLACE_WITH_COMPLETED_DCG_ID --margin-dn 2 \
  --output outputs/reference-sensor-dcg-remerge
```

정확한 명세, 제출 ID, 완료 record·summary, 링크·실행 시간·저장량을 JSON으로 보존한다. Wait timeout이어도 server job은 자동 취소되지 않는다. `*-submitted.json`의 ID로 이어서 조회할 수 있다. 동일 코드·명세의 제출 재사용은 Registry의 내용 기반 작업 식별을 따른다.

결과 UI는 `http://127.0.0.1:8780/reference-workshop.html?sensor_study=<run-id>`에서 연다. `field`, `sensor_case`, `sensor_a`, `sensor_b`, `sensor_interpolation`으로 선택 상태를 연결한다. 준비된 결과 탐색과 새 계산 제출은 구분된다.

준비 결과(2026-10-12):

- [Binning: 주변부 질감·잡음](http://127.0.0.1:8780/reference-workshop.html?sensor_study=run-4148f80302de47cca158f4575cfb13a5&sensor_case=case-023&sensor_a=native&sensor_b=charge_binning): 두 시야 × 다섯 차트 × 세 광량, 30조건.
- [Dual Gain: 주변부 밝기 단계](http://127.0.0.1:8780/reference-workshop.html?sensor_study=run-810f889b0eeb447faaa312ee8716d7ab&sensor_case=case-035&sensor_a=hcg&sensor_b=dcg): 두 시야 × 여섯 차트 × 세 광량, 36조건.

포화 부근에서 일부 CFA 채널만 무효이면 저장된 처리 영상에 색 얼룩이 나타날 수 있다. 해당 색은 유효한 복원 결과가 아니다. 화면은 저장된 재구성 유효 마스크로 무효 영역을 별도 표시한다. 원본 배열과 다운로드 미리보기는 바꾸지 않으므로 분석할 때 `common_valid_frames`를 함께 사용한다. 모드별 유효 영역이 다르면 그 영역에서 계산한 평균 신호·잡음을 같은 지지 영역의 순위로 해석하지 않는다. 밝기 단계 곡선과 유효 비율을 함께 본다.

## 저장·검증·제한

외장 SSD의 기존 `/Volumes/T7/CameraE2E` 저장소와 500GB 프로젝트 예산을 사용한다. 모드별 lossless NPZ에 RAW·전자·분산·유효 영역·LSC와 처리 결과를 보존하고 source revision·manifest·RAW 해시를 연결한다. Source 파일과 새 artifact의 소유 관계를 유지한다. DCG 재합성은 source paired RAW 해시를 참조하며 새로운 독립 취득으로 집계하지 않는다.

공간이나 source가 부족하면 명시적으로 실패한다. 해상도·취득 수·정확도를 자동으로 낮추지 않는다. 실행 시간은 원본 검증·수치 계산·저장·보고서를 포함한 벽시계 시간이며 실제 센서 readout·ISP latency가 아니다.

검증은 CFA 분리·신호 합산·pixel/node clipping 순서·공유 shot covariance·RAW 불변·LSC 한 번 적용·분산 gain²·실제 물리 중심·취소와 결과 바인딩에 집중한다. 모델은 기하 광학·평면층 sensor response·선언된 전기 가정의 범위를 유지한다. Quad Bayer remosaic, 실제 binning 회로, CDS/RTS/1/f 세부 잡음, charge-transfer 손실, 공간 crosstalk, 제품 mode 및 실측 calibration 검증은 포함하지 않는다.

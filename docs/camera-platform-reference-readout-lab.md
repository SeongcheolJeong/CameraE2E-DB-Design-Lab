# Reference Sensor Readout Design Lab

같은 특허 렌즈와 IMX462 구조 기반 픽셀 응답을 고정하고, 전기 readout 설정이
암부 잡음·계조·포화와 최종 영상에 주는 영향을 비교한다. **회로 행동 모델의 설계
가정 비교**이며 실제 IMX462의 회로 파라미터·제품 mode·실측 성능을 재현한 것이 아니다.
기존 [Binning·Dual Gain](camera-platform-reference-sensor-mode-lab.md)은 그대로 유지한다.

## 실행하는 비교

| Recipe | 기본 후보 | 질문 |
| --- | --- | --- |
| `gain_budget` | Analog×8/Digital×1, Analog×1/Digital×8, 양쪽 √8 | 같은 총 gain에서 후단 잡음·양자화·포화가 어떻게 달라지는가? |
| `noise_structure` | 독립 픽셀, 열 상관 잡음, 상관된 CDS | 같은 주변 분산에서도 줄무늬가 달라지는가? CDS 상관도는 잔여 잡음을 어떻게 바꾸는가? |
| `adc_headroom` | 10·12·14 bit | 같은 전압 범위에서 계조와 잡음이 어떻게 달라지는가? ADC와 well 중 어디가 먼저 포화되는가? |

`readout_lab` 설정은 `gain_total`, `post_gain_noise_e`, `cds_rms_e`,
`cds_correlation`, `column_fraction`, `adc_bits`다. 정확한 범위와 정규화된 값은
서버 template·저장 specification에 따른다. 설정을 편집하는 것과 계산 결과를
선택하는 것은 별도 동작이며, 새 설정은 제출 후 실제 완료된 결과에만 연결된다.

`post_gain_noise_e`는 **기준 conversion gain으로 환산한 후단 전압 잡음**을 뜻한다.
이를 기준 V/e로 곱해 V RMS를 고정한 뒤 analog gain을 변경한다. Analog gain을
올려도 광전자·shot noise나 앞단의 전자 기준 read noise가 줄어드는 것은 아니다.
Conversion gain V/e, analog 배율, digital 배율을 서로 바꿔 부르지 않는다.

현재 기준 자산의 full well 10,000 e, read noise 10 e, 12-bit ADC 등은 보존 모델의
가정이다. 기본 conversion gain은 정규화된 1 V full-well swing에서 생성된 값으로,
실제 FD capacitance를 추정한 값이 아니다. 앞단 잡음이 지배하면 analog gain이나
ADC 비트 수 증가의 이득이 작게 나오는 것이 정상이다.

## 입력·취득·공통 ISP

기본 source는 `run-d2105e38b3cd4f83b3550ebff21ca7fd`와 고정 manifest hash다.
기본 탐침은 중심·`outer-ne`, native 256×256 RGGB, 4 ms, 25°C, 8회 반복 취득이다.
광학·픽셀의 평균 전자율을 재사용하고 전기 조건마다 shot/read noise, clipping,
ADC와 정수 RAW를 실제 다시 계산한다. 이미 잡음·포화가 적용된 RAW를 배율 조절하지 않는다.

- `uniform`, `texture` 등은 저장된 광학 평균 응답을 읽는다.
- `dynamic_range`는 고정 렌즈·픽셀의 16단계 입력을 실제 계산한다.
- `dark`는 입사 광전자율 0의 명시적인 전기 암신호 시험이다. 암전류·읽기 잡음은 남는다.
- 반복 프레임 seed, 전기 모델, 실제 calibration, 포화 mask와 원본 RAW를 보존한다.
- 공통 전자 기준 색 행렬·native LSC·bilinear demosaic·고정 tone/white를 사용한다.
  프레임별 밝기 맞춤이나 자동 노출은 없다. 기존 고급 ISP의 제한을 삭제하지 않고,
  보정된 전자 배열을 받는 Sensor Mode adapter를 재사용한다.

CDS는 reset/signal의 픽셀·행·열 Gaussian 표본과 상관도에 따른 행동 모델이다.
기존 aggregate read noise와 중복 합산하지 않는다. 잡음 상관은 완전한 demosaic·CCM
출력 공분산으로 전파했다고 주장하지 않으며, 처리 후 잡음은 반복 출력으로 측정한다.
회로 대역폭, 1/f·RTS, settling, 전력, 실물 column ADC 결함은 이 데모에 포함되지 않는다.

## 결과 읽기

영상 A/B, signed 전자 기준 RAW preview, 원본 정수 RAW, 시간 잡음과 유효 영역을
같은 case에서 연다. Well·ADC·digital clipping은 서로 다른 단계로 구분한다.
PTC 진단은 균일장·밝기 단계의 동일 CFA site에서 평균과 픽셀별 시간 분산으로 계산한다.
자연 질감의 공간 분산을 read noise로 해석하지 않는다. 포화된 구간과 부족한 유효
표본은 표시하고, 그 구간으로 gain을 fitting하거나 더 좋은 SNR을 주장하지 않는다.
이는 시뮬레이션 계측이며 EMVA 준수 시험이나 실측 calibration이 아니다.

## SDK와 준비 실행

기존 `POST /api/v1/reference-workshop/sensor-mode-studies`에 세 recipe를 추가한다.
조회·case·파일·bundle·profile 저장 및 공통 wait/cancel은 기존 경로를 재사용한다.
버전 없는 기존 Binning/DCG 명세의 의미는 유지한다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=120) as client:
    job = client.submit_reference_readout_study(
        "gain_budget",
        settings={"gain_total": 8, "post_gain_noise_e": 4},
        fields=["center", "outer-ne"],
        charts=["uniform", "texture", "dynamic_range"],
        light_scales=[0.01, 1.0],
    )
    done = client.wait_reference_workshop(job["id"], timeout=7260)
    if done["status"] != "succeeded":
        raise RuntimeError(done.get("error") or done["status"])
    print(done["result"]["summary"]["report_url"])
```

```bash
PYTHONPATH=src .venv-platform/bin/python tools/run_reference_readout_lab.py
```

준비 실행기는 세 recipe를 순차 실행하고 제출 ID·명세·결과·비교 profile·링크를
`outputs/reference-readout-lab-20261012`에 기록한다. Timeout은 자동 취소가 아니며,
저장된 ID로 조회·취소할 수 있다. 원본·분산·mask·결과는 외장 SSD의 기존 500GB
프로젝트 예산으로 보존하고, 부족하면 명시적으로 실패한다. 계산 벽시계 시간은
실제 센서의 readout latency가 아니다.

준비 결과의 실행 상태·측정 비용·최종 링크는 해당 폴더의 `delivery.json` 및
`verification.json`에 기록한다. 파일이 존재한다는 이유만으로 성공으로 간주하지 않는다.

## 2026-10-12 준비 데모

8780 서버는 작업 트리의 구현을 실행한다. 아래 세 결과는 같은 고정 source에서
완료됐으며, 총 22조건·528개 native RAW와 비교 profile 3개를 보존했다.

| 완료 결과 | 조건 / 새 RAW | 계산 시간 | 결과 파일 |
| --- | ---: | ---: | ---: |
| [Gain 배분](http://127.0.0.1:8780/reference-workshop.html?sensor_study=run-c7ab86ab650b41f69a98647c0f0d0a79&sensor_case=case-008&field=outer-ne&sensor_a=digital_gain&sensor_b=analog_gain) | 12 / 288 | 276.7초 | 447.2 MB |
| [CDS·열 방향 잡음](http://127.0.0.1:8780/reference-workshop.html?sensor_study=run-437af9329d9f4fcf88d53d457f024c3b&sensor_case=case-003&field=outer-ne&sensor_a=pixel_iid&sensor_b=column_noise&sensor_stage=temporal_residual) | 6 / 144 | 145.7초 | 353.0 MB |
| [ADC 10·12·14 bit](http://127.0.0.1:8780/reference-workshop.html?sensor_study=run-406aa9328b8641c18cde9237a81d412b&sensor_case=case-003&field=outer-ne&sensor_a=adc_10bit&sensor_b=adc_14bit) | 4 / 96 | 117.3초 | 150.4 MB |

계산 시간 합계는 539.7초이며 제출·대기 시간과 구분한다. 위 결과의 논리 크기는
950.6 MB, SSD 할당 크기는 약 1.03 GB다. 공통 source와 취소한 초기 진단 run은
이 합계에 포함하지 않는다. 실행 중 발견한 ADC 끝 코드 반올림/추가 digital clip
집계 문제를 수정한 뒤 위 결과를 새로 생성했다. 취소된
`run-253b204a6b7c47fcaa73e28612537e57`은 인도 결과가 아니다.

추천 시연은 암신호의 **반복평균 제거 잔차** A/B다. 두 모델의 시간 잡음 크기가
약 2.14 e⁻ RMS로 비슷해도 열 공유 모델에서 줄무늬가 보인다. B를
`CDS CORRELATED`로 바꾸면 선언한 표본 상관도 0.8에서 약 1.05 e⁻ RMS의
결과를 확인할 수 있다. 회색이 0인 공통 전자 범위를 사용하며 원본 RAW 자체를
덮어쓰지 않는다. 이 결과는 실제 IMX462의 회로 성능값이 아니다.

ADC 비교는 전압 full scale을 고정한다. 비트를 늘려도 full well이 늘어나지
않으며, 현재 10 e⁻ 입력 read noise가 남아 있어 잡음 개선이 작을 수 있다.
Well·ADC·digital rail은 중복 가능한 진단이다. **추가 digital clip**은 ADC
정수 출력이 끝 코드가 아니었던 샘플이 digital 단계에서 rail에 닿은 비율이다.
모드별 유효 영역이 다르면 조건부 잡음 수치의 단순 비율로 우열을 판단하지 않는다.

다음 명령으로 원본/source 해시, 전체 case·mode 집합, 정수 RAW·전자 보정,
시간 분산, 저장 profile의 실행 연결을 다시 확인한다.

```bash
PYTHONPATH=src .venv-platform/bin/python tools/verify_reference_readout_lab.py
```

준비 결과 813개 파일 및 고정 source의 해시 검사를 통과했다. 관련 Python
테스트 149개와 Sensor Mode·ISP Lab·ISP tuning 브라우저 VM 검사를 통과했다.
재계산이 필요한 설정 변경과 이미 완료된 결과 탐색은 화면에서 구분한다.

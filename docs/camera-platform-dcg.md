# 공유 전하 HCG/LCG Runtime

이 문서는 `shared_charge_dcg_v1`의 구현 계약을 설명합니다. 대상은 동일한 전하를
이상적인 두 conversion-gain 경로로 비파괴 readout하는 **명시적 행동 모델**입니다.
특정 센서의 DCG 회로, LOFIC, charge transfer/reset 동작이나 실물 성능을 검증했다는
뜻이 아닙니다. 기존 `hdr_bracket_v1`은 서로 독립 reset한 노출 브래킷으로 유지됩니다.
배포·실제 UI 관측·설치 검증의 완료 여부는 해당 실행 보고서를 별도로 확인해야 합니다.

## 입력과 실행 경계

[dcg_runtime.py](../src/camera_platform/dcg_runtime.py)의
`SharedChargeDCGRuntime`은 기존 Temporal 요청에 다음 프로그램을 추가합니다.

```text
capture_program = {
  schema: "camera_dcg_capture_program_v1",
  electrical_model: <camera_shared_charge_readout_v1>,
  merge_profile: <camera_dcg_merge_v1 또는 null>
}
```

`default_capture_program(legacy_readout)`은 출처가 `estimated`인 예시 두 경로를
만듭니다. LCG의 평균 DN/e는 기존 readout에서, HCG conversion gain은 그 4배와
경로 charge ceiling 1/4로 설정합니다. HCG RMS read noise를 LCG의 절반으로 두고
cross covariance를 0으로 선언한 **예시 가정**이며 센서의 알려진 성능이 아닙니다.
기본 covariance 온도 범위는 25 °C 한 점입니다. 실제 모델은 적용 온도·경로 설정과
paired noise 근거를 명시해야 합니다.

- `recorded_control_sequence_v1.frames`의 각 항목은 **독립 획득 하나**입니다.
  각 획득은 같은 row window의 전하를 HCG/LCG 두 번 읽습니다. 노출 길이는 획득마다
  달라도 되지만, 둘을 독립 노출 두 개로 세거나 frame 축에 flatten하지 않습니다.
- 기존 Temporal의 clock·coverage·native crop·노출 겹침 검사를 그대로 적용합니다.
  Global/rolling shutter를 지원하며, 두 경로는 동일한 획득 row window를 공유합니다.
- 실제 적용된 acquisition `gain`은 정확히 1이어야 합니다. Conversion/analog/digital
  gain은 프로그램의 각 경로에 고정합니다. Commanded 설정은 기록으로 남고 실제 적용된
  설정만 획득을 구동합니다.
- 하나의 정적 분광 이미지, 두 경로, 동일한 native RGGB raster·packing·ADC bit depth만
  지원합니다. 경로별 black은 다를 수 있으며 각 calibration과 RAW format에 보존합니다.
  Waveform, 움직이는 입력, DCG+브래킷 다층 합성, 기존 RAW replay와 closed loop는 이
  Runtime의 지원 범위가 아닙니다.
- 기존 광학/PSF/RI/native EQE 적분으로 photoelectron 평균을 한 번 얻은 뒤
  [dcg_electrical.py](../src/camera_platform/dcg_electrical.py)를 호출합니다.
  Pixel area·EQE·RI·stop 손실을 두 경로에서 다시 곱하지 않습니다.

## 보존하는 축과 결과

| 배열/요약 | 의미 |
|---|---|
| `raw_groups.npy` | integer `[acquisition, readout, row, column]`; readout 순서는 `[HCG,LCG]` |
| `rgb_groups.npy` | `[acquisition, readout, row, column, RGB]`; 각 원본 RAW와 해당 경로 format, 기록된 ISP로 만든 미리보기 |
| `calibration_dark_e.npy` | `[acquisition,row,column]`의 선언된 dark 평균; JSON calibration에는 배열 이름과 acquisition index를 기록 |
| `electrical_shared_charge_e.npy` | simulator 내부 공통 전하 진단; 합성 및 RAW 예측 입력으로 전달하지 않음 |
| `electrical_*` | 실제 ADC, read noise, clipping truth, native PRNU/DSNU 진단. RAW에서 추정한 mask와 구분 |
| `row_start_ns.npy`, `row_end_ns.npy` | `[acquisition,row]`; 동일 acquisition의 두 경로가 공유하는 물리 노출 window |
| `merged_rate.npy` | 합성을 켠 경우 `[acquisition,row,column]`의 signed 전자율 |
| `usable_mask.npy`, `weights.npy` | `[acquisition,readout,row,column]`의 RAW 기반 검열 추정 및 합성 가중치 |
| `valid_mask.npy` | 적어도 한 경로를 사용할 수 있는 위치. 둘 다 무효인 radiometric NPY는 NaN |
| `merged_linear_rgb.npy`, `merged_rgb_valid_mask.npy` | RAW 재양자화 없는 float RGB와 보수적인 주변 3×3 유효 mask |
| `merged_rgb.npy`, `rgb.npy` | 명시적 display tuning을 적용한 acquisition별 preview와 첫 acquisition의 main preview |

`merge_profile=null`이면 두 RAW와 경로별 ISP preview만 생성합니다. `merged_*` 배열이나
합성 진단을 만들지 않으며 main preview는 첫 acquisition의 HCG입니다. 합성할 때는
`hdr.default_profile()`의 float demosaic/Reinhard display 설정을 결과 요약에 기록합니다.
이 설정은 DCG 합성 입력이 아니며, 경로별 configured ISP와 별도 표시 처리입니다.
미리보기에서만 음수 및 무효 위치를 검정으로 처리합니다. 선형 방사량은 mask와 signed
값을 유지하며 미리보기로 광학 MTF나 물리 색 정확도를 주장하지 않습니다.

Native 응답을 사용하면 계산과 결과의 site axis는 `[R,Gr,Gb,B]`이며 crop의 원래 CFA
위상을 보존합니다. 기존 legacy 응답은 `[R,G,B]`를 유지합니다. 네 사이트를 미리
평균하거나 Gb를 RGB의 B로 해석하지 않습니다.

`summary.acquisitions[]`는 acquisition ID/index, 공유 전하 group ID, 원래 recorded
frame, exposure, 두 readout ID와 timing을 갖습니다. 기존 형식의 `timing.frames`는
시각 계약을 보존하기 위한 것이며 RAW 결과를 별도 frame들로 세지 않습니다.
최상위 `frame_count`, `frames`, 단일 `raw_format` 대신 `acquisition_count`,
`readouts_per_acquisition=2`, `raw_formats`를 사용합니다.

## RAW 기반 예측과 합성

예측 metric은 `raw.channel_mean_dn_shared_charge`이고 축은
`[acquisition,readout,channel]`입니다. 실제 저장 RAW에서 경로별 black과 선언된 dark
평균의 DN을 뺀 signed channel 평균이며 noiseless expectation을 예측값으로 대신 쓰지
않습니다. 두 RAW format과 acquisition별 노출·온도 조건을 함께 보존합니다.

[dcg.py](../src/camera_platform/dcg.py)의 `merge_shared_charge_raw`에는 두 integer
RAW, exact calibration, merge profile만 전달합니다. Simulator 평균 전하, sampled
전하, clipping truth는 전달하지 않습니다. 공통 shot noise는 두 경로가 공유하므로
평균으로 반감하지 않으며, 가중치는 선언된 read covariance를 대상으로 한 nonnegative
최소 분산 해입니다. 결과의 분산 추정은 공통 shot plug-in과 read 항에 한정됩니다.
실제 양자화는 ADC/digital code로 실행하고 rounding의 절대 오차 bound는 stochastic
분산과 구별합니다. Total uncertainty 또는 실물 noise 적합성은 미확정입니다.

Gain-path agreement는 같은 전하를 읽은 경로 사이의 진단입니다. Temporal consistency,
움직임 보정이나 LFM 판정으로 표시하지 않습니다. RAW 경계 guard는 검열의 추정이며
read noise 때문에 안쪽으로 들어온 clipped sample을 항상 검출할 수는 없습니다.
Simulator의 clipping truth와 RAW 기반 usable mask는 서로 다른 근거입니다.

## 자원 한계와 검증 범위

제출 전 전체 acquisition×두 readout×native raster를 포함한 배열 예산을 계산합니다.
출력은 **배열별 128 MiB**, working array 추정은 **1 GiB** 상한입니다. Crop이 작아도
full native sensor PRNU/DSNU map의 비용을 포함합니다. 광학 field-PSF/native angular
work와 기존 Temporal의 frame·knot·spectral sample 제한도 유지하며 자동 축소하지 않습니다.
이 값은 측정된 프로세스 peak RSS 보증이 아닌 보수적 배열 예산입니다.

[test_dcg_runtime.py](../tests/platform/test_dcg_runtime.py)는 실제 전기·합성 모듈을
호출하며 두 RAW의 독립 scalar ADC 계산, paired noise와 read covariance 가중치,
RAW-only 입력, Gr/Gb 및 네 crop 위상, seed/reset, 정적 입력·clock·gain·온도·노출
경계, no-fusion, 공통 포화, 전체 native FPN 자원 예산을 확인합니다.
이 시험은 API/별도 Worker/설치 재시작/실제 UI 관측을 대신하지 않습니다. 그 수직 흐름은
원본 프로그램·Test·source snapshot·출력 blob을 결속한 별도 실행으로 검증해야 합니다.

일곱 모듈과 네 실행 방식은 유지합니다. 이 plugin은 Response-DB 방식 안의 새 획득
모델입니다. 사례에서는 고정한 sensor/program/Test, 두 RAW 근거, read-noise와 밝은 영역
headroom의 설계 판단, 남은 회로·교정 불확실성, 다음 paired dark/flat 실험을 연결합니다.

## v0.16.0.dev4 통합 경로

Factory의 **새 DCG 프로그램**에서 두 경로의 변환 이득·선형 상한·ADC와 입력 환산
읽기 공분산을 작성합니다. 일반 전기 입력을 바꾸면 기존 기본 가정이나 실측 표기를
그대로 재사용하지 않고, 수정한 값의 출처를 `estimated`로 기록합니다. 원본 버전은
부모 기록으로 보존하며 새 프로그램과 해당 시험을 등록합니다.

실험 화면은 **공유 전하 · HCG/LCG · DCG**를 선택합니다. 한 획득의 두 읽기를 서로
다른 시점의 프레임으로 바꾸지 않고, 획득 선택과 HCG/LCG/합성 선택을 따로 제공합니다.
저장된 각 경로의 RAW에는 그 경로의 형식과 보정 계약이 따라갑니다. 기존 단일 RAW
Replay는 두 경로를 묵시적으로 펼치지 않으며, 경로 추출 계약이 구현되기 전에는 거부합니다.

Python SDK의 `submit_dcg(...)`와 일반 Run API도 같은 불변 프로그램을 사용합니다.
큐 실행 전에 프로그램 원문·정규화 원문과 전체 고정 요청을 다시 검사합니다. 비교는
`acquisition × readout × channel` 축, 프로그램 ID/해시, native sampling과 두 RAW
형식이 일치해야 하며, 입력이 서로 동일하더라도 계약이 누락되면 거부합니다.

개발 소스의 선택형 `field_radiometry_mode=source_position_ri_v1`은 Lens의
소스 위치 상대 조도를 native 셀/서브셀에서 PSF **전**에 적용합니다. 같은
광전자 생성 전하를 HCG/LCG 두 경로가 읽으며, 정적 장면 검사에는 부모 영상뿐
아니라 서브셀 광량도 포함합니다. SDK의 `submit_dcg(...,
field_radiometry_mode="source_position_ri_v1")`로 선택할 수 있습니다. 모드를
생략한 기존 출력은 유지됩니다. 저장된 지도와 flux ledger, 자원 상한 및 frozen
원본 검사 범위는 [시간 소스 RI 계약](camera-platform-temporal-source-position-ri.md)을
따릅니다. 이는 합성 광학·전기 계산이며 실제 DCG 물리 정확도는 미검증입니다.

[통합 근거](../reports/camera_platform/dcg-v016/evidence-summary.json)는 수치·API·별도
Worker·실제 CUA 화면·프로세스 재시작의 범위를 구분합니다. 이 개발 소스의 기능은
기본 설치본 v0.15에 아직 포함하지 않았습니다. 다음 [고급 ISP 제안](camera-platform-advanced-isp-next-slice.md)은 별도 미구현 범위입니다.

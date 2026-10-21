# 다음 전기·HDR 확장: 한 전하를 공유하는 두 conversion-gain readout

2026-09-21 현재 소스를 읽고 작성한 **미구현 설계 제안**입니다. 아래 schema,
함수명, API 선택 항목과 수용 시험은 아직 제공되는 기능이 아닙니다. 현재
`hdr_bracket_v1`의 실행 증거를 이 제안의 DCG 실행·검증 증거로 승계하지 않습니다.
특정 제조사 센서의 회로, 획득 순서 또는 성능을 재현한다는 주장도 하지 않습니다.

다음 완결 사례는 **“같은 노출의 HCG/LCG에서 저조도 readout과 밝은 영역의
선형 범위가 어떻게 달라지고, 공유 잡음을 고려하면 합성의 이득이 얼마나 남는가?”**로
잡습니다. 하나의 전하 생성과 두 RAW 출력, 두 경로의 보정, RAW만 사용하는 합성,
사례의 판단·다음 실측까지 연결합니다. 기존 일곱 모듈과 네 실행 모드를 유지하고,
Response-DB Runtime에 제한된 전기 획득 모델을 추가하는 범위입니다.

## 현재 구현과 직접 재사용할 수 없는 부분

| 현재 소스에서 확인한 동작 | 다음 확장에서의 의미 |
|---|---|
| [`electrical.capture_electrons`](../src/camera_platform/electrical.py#L197)는 호출마다 기대 전하에서 Poisson 표본을 만들고, 공통 full well clipping 뒤 read noise·conversion/analog gain·post-gain noise·ADC·digital gain을 적용합니다. | 두 번 호출하면 두 독립 획득입니다. 동일 seed를 두 번 주는 방식도 공유 전하와 read-noise 공분산 계약을 대신하지 못합니다. |
| [`HDRBracketRuntime._execute`](../src/camera_platform/hdr_runtime.py#L317)는 프레임별로 `seed + index`를 사용하고, RAW 형식을 단일 stream으로 선언합니다. | `frame` 축을 HCG/LCG 축으로 재해석하면 시각·전하·난수 의미가 바뀝니다. |
| [`merge_raw_bracket`](../src/camera_platform/hdr.py#L164)는 실제 RAW와 9개 calibration 필드만 받고, 독립 노출 분산으로 가중치를 계산합니다. pair 진단도 `var_i + var_j`를 사용합니다. | RAW-only 경계와 signed 결과·censoring 정책은 재사용할 설계 원칙입니다. 독립 분산 합과 temporal consistency 진단은 그대로 사용할 수 없습니다. |
| [`validate_capture_program`](../src/camera_platform/hdr_runtime.py#L61)은 한 electrical model과 선택적 waveform/HDR profile만 허용합니다. HDR 합성에는 두 독립 노출, 하나의 정적 입력, PWM 정수 주기 노출이 필요합니다. | 새 획득 topology와 합성 schema를 명시하고 기존 v1의 해석·hash를 유지해야 합니다. |
| [`timing.validate_control_sequence`](../src/camera_platform/timing.py#L195)는 물리 픽셀 노출의 겹침을 거부합니다. [`ISP._input`](../src/camera_platform/isp.py#L228)는 단일 RAW stream만 받습니다. | 동일 획득 안의 두 readout과 서로 다른 획득의 겹침을 구별해야 합니다. 기존 검사를 완화해서 DCG처럼 보이게 만들지 않습니다. |

전기 v1의 `full_well_e`, conversion gain, ADC full scale이 독립 입력이라는 사실만으로
DCG가 구현된 것은 아닙니다. **한 번 관측한 전하의 공유 관계**가 데이터 계약과
실제 난수 생성, ADC 결과, 합성 오차 계산까지 유지되어야 합니다.

## 첫 구현에서 고정할 획득 모델

공개 명칭은 우선 **“공유 전하 · 두 conversion-gain 행동 모델”**로 표시합니다.
제안 schema는 `camera_shared_charge_readout_v1`, topology는
`ideal_shared_charge_two_conversion_paths`입니다. 범용 DCG 또는 회로 해석기라는
뜻으로 이름을 사용하지 않습니다.

한 acquisition의 각 native 픽셀에서 다음 순서를 한 번 실행합니다.

1. 기존 광학·native R/Gr/Gb/B 응답과 row window에서 광전자 평균을 적분합니다.
   EQE·pixel area·노출·stop 손실을 readout에서 다시 적용하지 않습니다.
2. 공통 PRNU, 온도 dark law와 DSNU로 전하 평균을 만들고, **공통 Poisson 전하를
   한 번만** 뽑습니다. 하나의 공통 charge capacity에서 clipping합니다.
3. 이 동일 전하를 HCG/LCG 두 전달 경로에서 읽습니다. 공통 전하가 경로별 clipping이나
   잡음에 의해 역으로 바뀌지 않는 이상화된 비파괴 두 readout을 가정합니다.
4. 선언된 입력 환산 read-noise covariance에서 두 잡음 성분을 함께 생성하고,
   경로별 선형 한계·conversion/analog gain·ADC·black-relative digital gain을 적용합니다.
   한 경로의 한계 처리는 다른 경로가 읽는 공통 전하를 변경하지 않습니다.
5. 두 integer RAW와 경로별 calibration을 보존합니다. 합성에는 이 RAW와 calibration,
   획득 그룹의 시각·geometry·출처만 전달합니다.

첫 범위는 두 경로만, 고정된 경로 gain 설정, 정적 분광 장면, waveform 없음으로
제한합니다. 여러 acquisition을 허용하더라도 각 acquisition끼리는 독립 reset이며
기존 노출 겹침 금지 조건을 유지합니다. DCG+순차 브래킷의 다층 합성, LOFIC,
서로 다른 photodiode, 전하 분할·전달 손실, gain 전환 중 reset/settling, CDS·RTS·1/f,
비선형 ADC/INL/DNL, bloom과 전기 crosstalk는 별도 계약으로 남깁니다.

HCG의 작은 선형 범위와 photodiode의 공통 capacity를 같은 수치로 묶지 않습니다.
제안 모델에는 `shared_charge_capacity_e`와 각 경로의
`input_linear_ceiling_e`를 구별합니다. HCG 경로만 먼저 clipping되어도 LCG가
공통 전하를 보존한다는 것은 **이번 행동 모델의 입력 가정**입니다. 실제 회로에서
그 가정이 성립하는지는 device mode·readout 순서·실측 근거가 필요합니다.

## 최소 입력과 축 계약

새 capture program 판별자 안에 아래 정보를 고정합니다. 최종 필드명은 구현 전
정규화 계약으로 확정하며, 기존 `camera_capture_program_v1`에 의미가 다른 필드를
조용히 끼워 넣지 않습니다.

| 묶음 | 필수 선언 |
|---|---|
| 공유 전하 | topology, 공통 capacity, dark/온도 법칙, 공통 native PRNU/DSNU와 pattern seed |
| 경로 | 정확한 `stream_ids=[HCG,LCG]`, 각각의 conversion gain V/e, 고정 analog gain, input linear ceiling e, ADC bit depth/full scale/black, digital gain |
| 잡음 | 정확한 stream 순서의 유한 대칭 PSD `read_noise_covariance_e2[2,2]`, 적용 온도/gain, 포함된 pre/post-amplifier 성분, source |
| 획득 | acquisition ID, shared-charge group ID, clock ID, 각 row의 동일한 노출 시작/끝, 적용 설정과 readout 순서·지연의 모델 가정 |
| RAW 그룹 | `axes=[acquisition,readout,row,column]`, 각 stream의 형식·단위, full sensor/crop/site origin, 원본 blob SHA |
| 보정 | 경로별 DN/e·black/white·최종 RAW linear floor/ceiling·dark 평균, read covariance와 rounding bound, 단위·출처·적용 범위 |

`HCG`와 `LCG`는 이름만 검사하지 않고 실제 conversion gain의 크기 관계를 검사합니다.
Conversion gain과 analog/digital gain은 구별합니다. 첫 구현은 동일한 native raster,
RGGB 원점, ADC bit depth·packing을 갖는 두 stream으로 제한하여 readout 모드 차이를
줄입니다. 서로 다른 black과 slope는 경로별 calibration에 명시합니다.

Read covariance 하나에 모델링한 모든 temporal read 성분을 입력 환산하여 포함하고,
기존 `read_noise_e`나 `post_gain_noise_v`를 추가로 더하지 않습니다. 경로별 총 noise와
공통 noise를 동시에 선언하는 중복 계약을 받지 않습니다. Gain·온도 변화 후 covariance가
유효한지는 domain으로 검사합니다. 측정되지 않은 covariance를 자동으로 0으로 채우지
않고, 명시적인 `estimated` 가정 또는 `missing`으로 남깁니다.

PRNU·DSNU는 acquisition마다 다시 뽑는 temporal noise가 아닙니다. 같은 sensor 모델과
full native 좌표의 패턴을 두 경로·crop·반복 획득에서 보존합니다. 경로별 fixed-pattern
gain/offset 차이는 첫 범위에서 제외하고 추가 calibration 모델로 확장합니다.

## 공유 잡음과 합성의 계산 경계

EMVA 1288 Release 4.0 Linear의 §2.4는 선형 센서 모델에서 전자 shot-noise의
Poisson 분산과 read/quantization 항을 구분합니다. §1.2에는 이미지 사이 temporal
noise 독립성 등의 적용 가정이 있습니다. 이를 **상관된 DCG 두 경로에 대한 표준
적합성 근거로 확장하지 않습니다**. 아래 식은 본 제안이 선언하는 행동 모델에서
도출한 계산 규칙입니다. [EMVA 공식 문서, §1.2·§2.4](https://www.emva.org/wp-content/uploads/EMVA1288Linear_4.0Release.pdf)

비포화·선형 구간에서 입력 전자 단위로 보정한 두 관측을 `z_H=q+n_H`, `z_L=q+n_L`로
정의합니다. `q`는 한 번 생성된 공통 전하, `n`의 covariance를 `R`이라 둡니다.
잡음 없는 dark 평균을 빼더라도 이미 발생한 dark shot noise가 사라지지는 않습니다.
공통 평균 전하를 `mu`라 하는 선언된 Poisson 모델에서는 양자화 전 covariance가
다음과 같습니다.

```text
C = mu * [[1,1],[1,1]] + R
Var(z_H - z_L) = R_HH + R_LL - 2*R_HL
Var(w_H*z_H + w_L*z_L) = mu + w^T R w,  when w_H+w_L=1
```

이는 포화 이전의 이상화된 모델 식입니다. 공유 shot noise는 두 경로를 평균한다고
반으로 줄어들지 않습니다. 경로 차이의 shot 성분은 상쇄되므로 기존 HDR의
`var_H+var_L`을 그대로 threshold로 쓰면 경로 불일치를 지나치게 관대하게 판정할 수
있습니다. 차이 검사는 `gain_path_agreement`로 명명하고, 장면 정렬·움직임 제거·temporal
consistency 판정으로 표시하지 않습니다.

첫 합성 함수 제안은 `merge_shared_charge_raw(...)`입니다. 기존
`merge_raw_bracket(...)`과 별도 진입점·schema를 갖습니다.

- RAW로부터 `z_j=(RAW_j-black_j)/dn_per_e_j`를 계산하고, 같은 dark 평균과 같은
  exposure로 signed photoelectron rate를 만듭니다. 원본 RAW는 변경하지 않습니다.
- 경로별 RAW floor/ceiling과 명시적 margin/read-noise guard로 censoring합니다.
  common capacity, 경로 선형 한계, ADC, digital clipping을 모두 반영해야 합니다.
  현재 구현에서 발견·보완한 digital gain < 1의 양수 RAW lower floor도 유지합니다.
- 두 경로가 유효하면 `0<=w_H,w_L<=1`, `w_H+w_L=1`에서 `w^T R w`를 최소화하는
  **선언된 nonnegative read-covariance 가중치**를 사용합니다. 공통 shot 항은 이
  최적화에서 상수입니다. 이는 전체 불확실도에 대한 최적 가중치라는 뜻이 아닙니다.
- 한 경로만 유효하면 그 경로를 선택합니다. 두 경로가 모두 무효이면 radiometric
  NPY는 NaN과 false mask, preview만 검정입니다. PSD이지만 퇴화한 covariance도
  일차원 constrained quadratic으로 처리합니다. 음의 완전 상관처럼 내부 최소점이
  존재할 수 있으므로 퇴화했다는 이유로 한 경로를 선택하지 않습니다. 목적함수가
  평평하여 해가 여럿일 때만 명시한 경로 우선순위로 tie-break하고 이유를 저장합니다.
  보고하지 않은 jitter를 더해 역행렬을 만들지 않습니다.
- RAW에서 추정한 공통 shot 항과 `w^T R w`의 합은
  `shared_shot_read_variance_estimate`로 표시합니다. calibration 오차·PRNU·DSNU·model
  mismatch·양자화 covariance를 포함한 total uncertainty 또는 신뢰구간은 아닙니다.
  이 식의 단위는 e²이며 rate 분산에는 공통 exposure²로 나누어 (e/s)²로 변환합니다.

RAW 경계 guard는 검열의 보수적 추정입니다. 공통 capacity에서 잘린 전하가 read
noise 때문에 경계 안으로 들어오는 경우까지 RAW만으로 완벽하게 판별할 수는
없습니다. simulator의 실제 clipping 진단과 RAW로 추정한 usable mask를 별도 이름으로
저장하고, 후자를 포화 truth 또는 복구 보증으로 표시하지 않습니다.

**양자화는 별도의 차단점입니다.** 두 ADC가 같은 전하를 양자화하므로 rounding
오차까지 독립이라고 둘 수 없습니다. 기존 1/12 근사 분산을 두 경로에 복사하여
합성 SNR을 계산하지 않습니다. 첫 범위는 각 경로의 실제 ADC/digital rounding을
그대로 실행하고, 비검열 선형 구간의 입력 환산 rounding 절대 오차 한계를 따로
보존합니다. 합성 rounding bound는 nonnegative 가중치로 결합합니다. 이 한계는
확률 분산과 합산하지 않으며, low-signal bias와 경로 간 양자화 상관은 독립 계산으로
확인합니다. 양자화까지 포함한 통계 모델은 후속 signal-dependent paired calibration
또는 별도 검증된 joint noise 모델이 필요합니다.

합성 입력에는 `mean_total_charge_e`, Poisson 표본 `q`, clipping truth mask, noiseless
reference를 허용하지 않습니다. 해당 배열은 simulator 진단과 독립 oracle 시험용으로만
별도 저장합니다. Calibration에는 sensor/model 상수와 독립 보정 정보만 들어가며,
실행 중 생성된 truth 값을 보정이라는 이름으로 전달하지 않습니다.

## 실제 실행까지 필요한 최소 변경

| 경계 | 변경할 내용과 완료 조건 |
|---|---|
| 전기 계산 | 새 공유 전하 모듈에서 charge generation과 두 path readout을 분리합니다. 기존 `capture_electrons`의 결과·seed·v1 hash를 유지합니다. 모든 covariance/숫자/shape 검사를 난수 생성 전에 수행합니다. |
| 시간·capture program | 새 acquisition schema로 노출 그룹과 readout을 구별합니다. 같은 그룹의 두 readout은 새로운 노출이 아니며, 그룹 사이에는 기존 overlap 검사를 유지합니다. clock 불일치와 임의 stream 순서 변경을 거부합니다. |
| Runtime | 제안 plugin `shared_charge_dcg_v1`을 Response-DB 실행 방식에 연결합니다. 기존 광학/native 사이트 전하를 한 번 구하고 두 stream을 생성합니다. noise topology와 covariance 모델 버전이 cache key에 들어갑니다. |
| RAW·ISP | HCG/LCG 원본은 별도 NPY와 stream descriptor로 보존합니다. 현재 `[frame,row,col]`에 flatten해서 시간축처럼 저장하지 않습니다. 기존 ISP는 선택된 단일 stream에만 적용하고, 합성 float Bayer/linear RGB는 재양자화 없는 별도 경로로 preview를 만듭니다. |
| Registry·검증 | Program 원본/정규화 JSON, calibration/covariance, Sensor/Lens/ISP/Test, group/stream 순서, 환경·코드·seed·원본 RAW를 hash로 결속합니다. 제출·Worker·cache 재사용·재개방에서 같은 바인딩을 확인합니다. |
| Test·관측 | `acquisition/readout/site` 축을 명시합니다. `[R,Gr,Gb,B]`를 유지하고 기존 RGB 또는 단일 stream 측정과 자동 비교하지 않습니다. paired RAW 분석은 실제 코드에서 산출하며 expectation을 측정값처럼 쓰지 않습니다. |
| API·SDK·UI | 사람과 Agent 모두 같은 신규 program/test/run 계약을 사용합니다. 기존 v1 요청은 그대로 유지하고 unsupported DCG를 기존 HDR에 자동 fallback하지 않습니다. |

현재 [`capture_programs.create`](../src/camera_platform/capture_programs.py#L30)는 단일
electrical ADC와 Assembly RAW 형식을 비교하며, `service.py`·`imaging.py`·`isp.py`에도
단일 stream 경계가 있습니다. 따라서 계산 함수만 추가한 상태는 완료로 인정할 수
없습니다. 원본 RAW 그룹의 표현과 Composer·replay·export의 명시적 수용/거부를 함께
구현해야 합니다. 첫 범위에서 지원하지 않는 closed-loop AE/AWB·기존 RAW replay에는
수행 전 오류와 이유를 반환합니다.

계산 자원은 acquisition 수 × 2 readouts × native pixel 수 × 보존 배열 dtype으로
계산합니다. 공통 optical rate를 경로 수만큼 중복 실행하지 않습니다. covariance는
2×2 scalar 모델로 시작하고 큰 per-pixel covariance LUT는 받지 않습니다. batch와
예상 working/output byte 한도를 제출 전 고정하며 한도 초과 시 crop·샘플을 몰래
줄이지 않습니다.

## 독립 수용 시험과 저장 근거

1. **한 전하·두 실제 RAW.** Noise off에서 지정 전하·gain·black·ADC의 독립 scalar
   식과 두 integer RAW가 양자화까지 일치해야 합니다. Noise on에서는 같은 acquisition의
   charge를 한 번 생성한 사실과 별도 read 성분을 진단 배열로 확인합니다. 독립 브래킷
   경로가 같은 모델로 잘못 분류되지 않아야 합니다.
2. **공분산과 가짜 SNR 개선 검출.** 비포화 uniform target, PRNU/DSNU off, 명시한
   양·음의 cross covariance로 반복 획득합니다. 독립 코드로 sample covariance와
   경로 차이 분산을 계산합니다. 허용 오차와 샘플 수를 실행 전에 고정하며, 공유 shot이
   평균으로 반감되는 잘못된 구현이 실패해야 합니다. Pre-ADC moment와 실제 양자화 RAW의
   결과를 구분하고 rounding bound를 포함한 별도 oracle을 사용합니다.
3. **HCG 포화와 LCG 회복.** HCG path 한계만 넘은 영역은 LCG로 유지되고, 공통 charge
   capacity를 넘으면 양쪽 모두 복구 불가여야 합니다. ADC white에 도달하지 않은 hidden
   clipping, digital attenuation lower floor, 한 경로 유효·모두 무효를 검사합니다.
4. **RAW-only 합성.** 저장된 두 RAW와 calibration만으로 독립 구현한 가중치·signed
   rate·mask·read covariance 항을 비교합니다. 원본 expectation/truth mask를 오염해도
   RAW와 calibration이 같으면 합성이 같아야 합니다. 원본 RAW SHA도 불변이어야 합니다.
5. **Native와 시간.** Gr/Gb가 다른 입력, 네 crop 위상, 동일한 row windows, group/stream
   ID를 실제 RAW까지 확인합니다. 불일치한 clock·crop·경로 순서·exposure는 거부합니다.
   1개 acquisition/2개 readouts를 2개의 독립 frame으로 세면 실패해야 합니다.
6. **API·Worker·재시작.** 격리된 상태에서 immutable Sensor→Program→Test→Run을 실제
   API와 별도 Worker로 실행합니다. source/calibration/RAW 손상, wrong frozen program,
   stream 교체, stale Test는 제출·실행·cache에서 거부합니다. 재시작 후 같은 ID와 모든
   원본 artifact byte hash, evidence ZIP을 확인합니다. 이전 브래킷 실행을 새 실행으로
   기록하지 않습니다.
7. **UI.** 새 program 저장, 정확한 revision 선택, Test 고정, 실행, HCG/LCG 선택, shared
   saturation·gain-path agreement·미확정 항목 표시, 근거 download와 재개방을 실제 화면에서
   검증합니다. form/state 시험만으로 UI 관측을 주장하지 않습니다.

관측 가능한 최소 결과는 두 RAW·두 preview, 공통 획득 시각, 보정된 경로별 rate,
합성 float Bayer/linear RGB, censor/선택 mask, read covariance 기여와 rounding bound,
공분산 진단 및 SHA가 있는 실행입니다. 제조사 DR 수치, 움직임 제거, LFM 인증,
실물 noise 적합성은 이 수용 기준에 포함되지 않습니다.

## 사례 중심 UI와 일곱 모듈·네 실행 모드

사례에는 “어두운 영역 read noise”와 “밝은 영역 linear headroom” 요구사항을 따로
두고, 선택한 sensor/program revision과 정확한 Test를 연결합니다. 결과 화면의 첫
줄에는 **“동일 노출 · 공유 전하 · HCG/LCG 두 readout”**을 표시합니다. 기존 순차
브래킷을 선택하면 “독립 reset 노출”로 표시합니다. Exposure나 gain을 변경하면 새
program/Test revision이 필요하며 이전 사례의 근거를 현재 설정처럼 집계하지 않습니다.

| 기존 모듈 | 이번 slice에서 맡을 역할 |
|---|---|
| Asset Registry | 센서 전기 mode·paired calibration·원본 RAW group·출처/적용 범위 보존 |
| Model Factory | 명시적 행동 모델 후보와 향후 paired dark/flat 분석 후보 생성; 계산/실측 출처 분리 |
| Camera Composer | optical/native-site 계약, gain mode, 두 stream 형식, clock·전하 공유·적용 범위 검사 |
| Simulation Runtime | 동일 전하의 두 전기 경로와 RAW-only 합성 실행 |
| Validation Center | 경로별 잔차·공분산·합성 bias·포화 경계, 독립 holdout과 비교 가능성 검사 |
| Experiment & Optimization | 고정 scene/exposure에서 gain ratio·path headroom·read covariance 민감도 비교; 단일 RGB 점수 대신 trade-off |
| Agent & Release Control | 동일 API로 근거가 연결된 실험을 수행하고 설계 판단·남은 문제·다음 실측을 기록; 계산 성공으로 물리 승인하지 않음 |

네 실행 방식도 그대로 유지합니다. Quick Estimate는 명시한 선형 영역에서 후보
계산을 제공하고, Response-DB는 본 slice의 실제 두 RAW 획득을 담당합니다.
Detailed Characterization에는 후속 paired 측정/회로별 분석을 연결하며,
Surrogate Prediction은 검증된 DCG 데이터가 없으면 지원되지 않는다고 표시합니다.
DCG라는 다섯 번째 제품 실행 방식을 만들거나 네 방식 모두 완성된 것처럼 표기하지
않습니다. 실측 데이터는 이 네 계산 방식과 구별한 근거로 등록합니다.

## 구현 전에 고정할 차단점과 다음 실측

가장 중요한 결정은 대상이 **이상화된 같은 전하의 두 readout**인지, 실제 모드의
charge transfer·reset·추가 storage를 포함해야 하는지입니다. 후자라면 현 제안의
topology로 모사했다고 주장하지 않고 다른 입력 계약을 설계해야 합니다. Vendor의
“DCG” 이름이나 conversion-gain 비율만으로 topology와 noise 공유 비율을 추정하지
않습니다.

두 번째는 paired read covariance의 기준입니다. 같은 온도·노출·gain·mode에서
획득한 paired dark/flat RAW, stream 시각·순서, black/linearity와 clipping 구간,
camera/serial·교정 정보가 필요합니다. 모델 보정용 데이터와 검증용 데이터는
분리하고, RGB SNR만으로 covariance·gain·full well을 동시에 맞추지 않습니다.
근거가 없을 때도 명시적 합성 모델의 구현 검증은 가능하지만 실물 예측은 미검증입니다.

세 번째는 joint quantization과 calibration 불확실도입니다. 실제 원본 RAW의
경로별 이산 code 관계가 강하면 단순 Gaussian/균일 quantization 가정은 맞지 않을
수 있습니다. 이 첫 slice는 그 한계를 숨기지 않고 실제 rounding과 독립 oracle,
별도 bound를 유지하는 수준까지 구현합니다. 그다음 사례 실험은 dark/flat 수준별
paired residual을 측정하여 **read covariance 모델의 부족인지, 양자화/비선형성인지,
실제 charge topology 차이인지**를 분리하도록 연결합니다.

출시나 installed primary 변경은 이 설계 문서의 작업 범위가 아닙니다. 구현 시에는
새 코드·새 설치물·새 실행 근거를 결속하고 기존 v1 RAW/HDR regression 및 원본
기록 보존을 확인한 뒤 별도의 배포 절차를 따릅니다.

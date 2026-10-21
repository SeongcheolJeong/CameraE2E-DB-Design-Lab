# 비선형 전하 응답과 RAW 역보정 — v0.16.0.dev10

상수 conversion gain만 사용하던 경로에 **단조 전하→전압 곡선과 그 RAW 역보정**을
함께 추가했다. 순차 HDR, 공유 전하 HCG/LCG, 기준 노출 보존 HDR에 각각 v2 계약을
사용한다. 기존 v1 모델을 자동 변환하거나 실측 교정으로 승격하지 않는다.

이 문서는 구현된 계산 계약을 설명한다. 전체 Camera Simulation·Validation 플랫폼의
완성이나 고객 배포 준비, 실물 센서 정확도 검증을 뜻하지 않는다.
[이전 제안](camera-platform-nonlinear-readout-next-slice.md)은 당시의 미구현 계획으로 남긴다.

## 구현 경로와 지원 범위

| 경로 | 프로그램 schema | 실행 범위 |
|---|---|---|
| 독립 순차 노출 | `camera_capture_program_v2` | 프레임별 독립 reset·Poisson·RAW; 합성 시 정적 장면과 두 개 이상 노출 |
| 공유 전하 HCG/LCG | `camera_dcg_capture_program_v2` | 한 번 샘플링한 전하를 두 경로가 읽고, 전압 잡음 공분산을 사용 |
| 기준 노출 보존 HDR | `camera_reference_hdr_capture_program_v2` | RAW가 일치하는 곳만 합성; 불일치 주변은 기준 노출로 복귀; 무효 기준 노출은 복구하지 않음 |

실행 adapter의 기존 ID는 유지하며, 프로그램·전기 모델·합성 schema로 v1/v2를 구분한다.
서로 다른 버전의 모델과 합성 프로파일을 섞으면 거부한다.
([프로그램 분기](../src/camera_platform/capture_programs.py),
[순차 Runtime](../src/camera_platform/hdr_runtime.py),
[DCG Runtime](../src/camera_platform/dcg_runtime.py),
[기준 노출 Runtime](../src/camera_platform/reference_hdr_runtime.py))

순차 HDR의 PWM은 장면 전체를 변조하며 합성 시 각 노출이 광원 주기의 정수배여야 한다.
합성을 끈 경로에서는 프레임별 주기광 진단을 보존한다. 기준 노출 HDR은 `null` 또는
`constant` 광원만 지원하며, 변화하는 영상의 정합·완전한 deghosting은 하지 않는다.
DCG는 정적 장면, 프로그램에 고정된 경로별 gain을 사용하고 획득 설정의 gain은 1이다.
세 경로 모두 제품별 LFM 알고리즘이나 일반 움직임 HDR을 구현했다는 의미는 아니다.

## 전하→전압 곡선

[`charge_voltage_response.py`](../src/camera_platform/charge_voltage_response.py)의
`camera_charge_voltage_response_v1`은 다음을 명시한다.

- `charge_e`, `signal_voltage_v`: 2..256개 같은 길이의 JSON 배열.
  원점 `(0,0)`에서 시작하고 두 축 모두 엄격히 증가하는 구간 선형 곡선이다.
- `units`: `{"charge":"electron","voltage":"V"}`.
  전압은 analog gain·black pedestal 이전의 신호 전압이다.
- `valid_domain`: 온도와 analog gain의 포함 범위. `[25,25]` 같은 고정 조건도 허용한다.
  실행 capacity까지 곡선이 덮어야 한다.
- `signed_inverse`: 음수 `minimum_voltage_v`와 `method="origin_tangent"`.
  black 아래 readout residual의 역좌표에만 첫 구간 기울기를 연장한다.
- `source`: `estimated`, `simulated`, `measured` 중 출처 종류와 설명.

예를 들어 `(0 e,0 V) → (1000 e,0.1 V) → (2000 e,0.15 V)`이면
500 e는 0.05 V, 1500 e는 0.125 V다. 물리 순방향 입력은 비음수이며 정의된 범위
밖을 자르거나 외삽하지 않는다. Signed 역좌표는 음수 전자가 생긴다는 물리 주장이 아니다.

구현 상한은 곡선 전하 `1e13 e`, 전압 `1e6 V`, 구간 기울기 `1e-15..1e3 V/e`다.
온도 `−273.15..1000 °C`, gain `1e-6..1e6`은 입력의 수치 한계이며 실제 센서의
검증된 동작 범위가 아니다. 실제 요청은 모델에 작성된 더 좁은 유효 범위를 따라야 한다.
중복·평탄·감소 knot, NaN/Inf, bool, 단위·shape 불일치와 미지원 설정은 거부한다.

## 잡음과 취득 순서

[`nonlinear_readout.py`](../src/camera_platform/nonlinear_readout.py)의
`camera_electrical_readout_v2`는 다음 순서를 고정한다.

1. 광전하 평균에 고정 photo-response와 암전류 평균을 적용한다.
2. 잡음 활성화 시 Poisson 전하를 한 번 샘플링하고 full well에서 제한한다.
3. 전하→전압 곡선을 적용한다.
4. `conversion_output_before_analog_gain` 위치의 전압 읽기 잡음을 더한다.
5. Analog gain → nearest-even ADC 양자화·범위 제한 → black 기준 digital gain과
   두 번째 nearest-even 양자화·범위 제한을 적용한다.

읽기 잡음은 `V²` 분산이며 `all_temporal_read_components`라고 선언한 항 전체다.
기존 v1의 input-electron noise를 여기에 다시 더하지 않는다. 잡음 위치 자체도
행동 모델의 가정이다. 잘못된 온도 암전류 법칙은 센서 배열 생성 전에 거부한다.

[`nonlinear_dcg.py`](../src/camera_platform/nonlinear_dcg.py)는 공통 전하를 한 번만
샘플링한 뒤 HCG/LCG의 각 capacity·곡선·ADC를 적용한다. 경로 순서가 고정된 `V²`
공분산을 사용하며, 공통 shot-noise 분산을 독립 두 노출처럼 평균으로 줄이지 않는다.
이는 손실 없는 공유 전하 읽기 가정이며 실제 DCG 회로의 charge transfer·settling
또는 파괴적 readout을 모델링한 것은 아니다.

## RAW만 사용하는 역보정과 수치 한계

Decoder는 integer RAW와 고정 calibration만 받는다. Calibration은 모델·획득 설정의
hash, dark map의 little-endian float64 byte hash, 원래 `[H,W]` shape와 encoding을
검사한다. Full sensor 크기·crop origin·노출·gain·온도·잡음 활성 여부도 고정된다.
시뮬레이터의 noiseless charge, sampled charge, clipping truth는 역보정·합성 입력이 아니다.

실제 digital quantizer에서 해당 RAW code를 만드는 ADC 정수 범위를 찾은 다음,
ADC 전압 구간을 곡선의 전하 구간으로 역변환한다. 건너뛴 code, ADC endpoint,
capacity guard, 선언된 signed/positive 역범위 밖은 mask와 NaN으로 남긴다.
읽기 잡음 때문에 포화가 숨을 수 있으므로 이 검열이 물리적 비포화를 증명하지는 않는다.

점 추정값은 **확장 전 전압 구간 중심을 역변환한 값**이다. 전하 양 끝점의 평균과
다르며, 수치 여유를 추가한다고 점 추정값이나 local inverse slope를 이동시키지 않는다.
극단적으로 작은 전압 기울기가 반올림 오차를 증폭할 수 있어 decoder는 전압 scale에
따른 `16 × float64 epsilon` 여유와 charge 연산 여유를 범위에 추가한다.
`numerical_voltage_allowance_v`, `numerical_charge_allowance_e`로 그 크기를 기록한다.
이것은 명시적인 계산 허용오차이며 모든 플랫폼에 대한 형식적 IEEE interval proof는 아니다.

양자화·수치 구간은 확률 분산으로 바꾸지 않는다. 전압 읽기 잡음은 구간 중심의 역기울기로
국소 변환하고 shot noise는 plug-in 추정값을 쓴다. Knot 통과 mask를 보존하지만,
곡선 경계 부근의 inverse bias, 검열·가중치 선택 bias, 교정 오차와 전체 불확실성은
확립하지 않았다. 반환된 구간은 양자화된 잡음 포함 전압의 signed 역좌표이며,
잡음 이전의 참 광전하에 대한 신뢰구간이 아니다.

[`nonlinear_hdr.py`](../src/camera_platform/nonlinear_hdr.py)와
[`reference_hdr.py`](../src/camera_platform/reference_hdr.py)는 이 보정을 공유한다.
유효 노출의 값만 합성하므로 `0 × NaN`이 유효 출력을 오염시키지 않는다. 모두 무효이면
물리 배열은 NaN이고 표시 영상만 검정이다. 시간 일치 판정은 진단이며 장면 정합 증거가 아니다.

## 미리보기와 기존 고급 ISP의 구분

V2 프로그램은 다음 미리보기 계약을 명시해야 한다.

```json
{"method":"calibrated_charge_fixed_color_v1","normalization":"full_well_e","isp_profile_role":"fixed_color_settings_only"}
```

[`nonlinear_isp.py`](../src/camera_platform/nonlinear_isp.py)는
`(decoded total_charge_e − dark_e) / full_well_e` → native CFA bilinear → 고정 WB·CCM·tone을
실행한다. 네 crop phase를 유지하고, 반사 경계의 3×3 RAW 이웃이 모두 유효한 곳만
RGB 계산값으로 인정한다. 음수 선형 값은 보존하고 tone 입력에서만 표시 범위를 제한한다.

여기에 쓰이는 `camera_isp_profile_v1`은 **고정 색 설정의 출처**다. 결과의
`executed_graph`와 `color_settings_profile`을 따로 기록하며 전체 v1 graph를
실행했다고 주장하지 않는다. `camera_isp_profile_v2`는 이 미리보기에서 거부한다.
기존 선형 RAW용 고급 ISP·LSC·denoise·tuning Study 기능이 새 비선형 RAW에 자동으로
적용되는 것은 아니다. 비선형 RAW의 일반 재처리·고급 ISP 탐색은 별도 확장 범위다.

호환 배열 이름 `black_subtracted_normalized_frames`도 이 경로에서는 DN black subtraction이
아닌 보정된 signed 광전하/full-well을 뜻한다. 요약에 이 의미와 모든 배열 단위를 기록한다.
네이티브 사이트 비교용 관측 지표는 별도로 **실제 RAW−black DN 평균**이며 dark 응답을
빼지 않는다. 이 지표와 보정된 전하율을 혼동하거나 DN을 광자 수로 해석하면 안 된다.

## 작성·근거·검증 상태

Model Factory는 세 종류의 비선형 초안, 전하·전압 knot 표, 원본 JSON 편집을 제공한다.
잡음·적용 범위·출처·signed 역범위를 검토한 뒤 새 프로그램 버전으로 등록한다.
기본 초안은 기존 수치를 scale로 삼아 작성한 **estimated 예제**다. UI의 물리 parameter
수정도 estimated 출처로 기록하고 부모 기록을 남긴다. 측정 곡선 fitting이나 자동 교정은
이 범위에 포함되지 않는다.

독립 검토의 범위와 재현 사례는
[`review.json`](../reports/camera_platform/nonlinear-readout-v016/review.json),
집중 시험은 [`test_nonlinear_review_cases.py`](../tests/platform/test_nonlinear_review_cases.py)에 있다.
전체 회귀·HTTP·화면 검증의 최종 판정은 해당 버전의 별도 결과 기록을 따른다.

`physical_validation=not_established`, 전체 uncertainty는 `unknown`을 유지한다.
출처를 `measured`로 기록하는 것만으로 센서 정확도가 검증되지는 않는다. 실제 검증에는
원본 RAW·장비·설정·온도·gain과 독립 평가 구간의 residual 및 dark/flat 근거가 필요하다.
일반 TCAD, 회로 settling/reset/CDS, trapping, blooming, charge-transfer dynamics,
실물 colorimetry와 제품별 LFM 성능은 여전히 별도 검증·구현 대상이다.

## dev10의 재현 가능한 계산 근거

최종 `tests/platform` 회귀는 **3,177개**, Node UI 경계 검사는 **92개**를 통과했다.
별도 집중 검사 수를 중복 합산하지 않는다. 실제 HTTP API와 별도 Worker의
프로그램 등록 → 고정 Test → RAW 획득 → 보정·합성 → 설계 Case 연결은
[489개 검증](../reports/camera_platform/nonlinear-readout-v016/http-final/http-verification-before-restart.json)을
통과했다. 순차 HDR, 기준 노출 HDR, DCG 세 방식과 응답 구간 비교용 추가 실행을 보존했다.

[저장 RAW 비교 그림](../reports/camera_platform/nonlinear-readout-v016/nonlinear-readout-comparison.png)은
동일한 작성 장면의 4/8 ms 노출에서 두 응답 구간을 통과한 유효 픽셀 134개를 사용한다.
전자율 차이의 중앙값은 비선형 역보정 **0.004%**, 원점 기울기를 전체 구간에 잘못 적용한
복원 **−10.86%**였다. 새 역보정의 절대 차이 95백분위는 **3.36%**다.
잡음·양자화·검열이 포함된 두 RAW의 조건부 일관성 비교이며, simulation truth 오차나
실물 정확도를 평가한 숫자가 아니다. 분석에는 저장 RAW와 hash로 묶인 calibration만 썼다.

기존 v1·dev9 Study의 전체 기록과 bundle 내부 파일은
[동일성 검사](../reports/camera_platform/nonlinear-readout-v016/legacy-readback/legacy-study-readback.json)를
통과했다. 새 QA의 [백업·복원](../reports/camera_platform/nonlinear-readout-v016/backup-restore-verification.json)은
31개 기록·5개 작업·46개 audit·341개 blob을 그대로 보존했다. 최종 재시작·UI·소스 묶음과
[남은 문제](../reports/camera_platform/nonlinear-readout-v016/known-issues.json)는
[통합 근거 요약](../reports/camera_platform/nonlinear-readout-v016/evidence-summary.json)을 따른다.

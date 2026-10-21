# 다음 전기·HDR 확장 제안: 비선형 전하 응답과 RAW 역보정

**보존된 dev9 시점의 설계 제안이다.** 아래 내용은 구현 전 판단 근거이며 현재 완료 상태가 아니다.
정적 단조 응답·RAW 역보정·세 v2 촬영 방식의 dev10 구현 범위와 남은 한계는
[비선형 readout 문서](camera-platform-nonlinear-readout.md) 및
[검증 근거](../reports/camera_platform/nonlinear-readout-v016/evidence-summary.json)를 따른다.

dev9 전기 모델은 전하량에 상수 conversion gain을 곱하고,
HDR/DCG는 RAW를 상수 DN/e로 나누어 전자율을 복원한다. 순방향 gain만 비선형
곡선으로 바꾸면 합성 결과가 잘못된다. 다음 범위는 **비선형 순방향 응답, 명시적
noise basis, RAW 역보정을 함께 연결한 설계 비교 사례**로 제안한다.
일곱 플랫폼 모듈과 네 실행 방식의 전체 목표를 이 범위로 대체하지 않는다.

1. **현재 근거와 바꿀 지점.**
   [`electrical.capture_electrons()`](../src/camera_platform/electrical.py#L204)는
   [`charge × volts_per_e`](../src/camera_platform/electrical.py#L239)를 사용한다.
   [`dcg_electrical.capture_shared_electrons()`](../src/camera_platform/dcg_electrical.py#L241)도
   [`path_charge × adc_slope`](../src/camera_platform/dcg_electrical.py#L289)로 변환한다.
   [`hdr.merge_raw_bracket()`](../src/camera_platform/hdr.py#L164)와
   [`dcg.merge_shared_charge_raw()`](../src/camera_platform/dcg.py#L184)의 역변환은
   각각 [상수 DN/e](../src/camera_platform/hdr.py#L198),
   [경로별 상수 DN/e](../src/camera_platform/dcg.py#L210)에 의존한다.
   [`reference_hdr.merge_reference_raw_bracket()`](../src/camera_platform/reference_hdr.py#L44)도
   기존 HDR의 방사량·분산 결과를 사용하므로 같은 역보정 계약을 따라야 한다.

2. **추가할 모듈과 계약.** 새 `charge_voltage_response.py`에
   `validate_response()`, `evaluate_voltage(charge_e, response)`,
   `invert_voltage_interval(low_v, high_v, response)`를 제안한다.
   `camera_charge_voltage_response_v1`은 전하 `electron`과 신호 전압 `V`의
   2..256개 knot, 구간 선형 보간, 적용 온도·gain 설정, source를 명시한다.
   물리 입력은 수집 이후의 비음수 전하이며 `(0 electron, 0 V)`를 포함한다.
   전하와 전압 knot는 모두 엄격히 증가해야 한다. 검증된 입력 구간 밖을 묵시적으로
   외삽하지 않고, 적용 well/path capacity를 곡선이 덮지 못하면 실행 전에 거부한다.

3. **모델 가정과 버전.** 이 곡선은 전하량에 따라 달라지는 정적·단조 전기 응답을
   표현하는 행동 모델이다. 전압은 black pedestal을 더하기 전 신호 성분으로
   정의한다. 새 electrical/capture/calibration schema로 도입하며 기존 v1의
   모델 의미·seed·RAW·hash는 바꾸지 않는다. 기존 상수 gain과 noise를 임의의
   비선형 모델이나 실측 교정으로 자동 승격하지 않는다.

4. **순방향 clipping과 noise 위치.** 새 경로는 수집 평균과 dark → 한 번의
   Poisson draw → well/path clipping → 비선형 전하-전압 응답 → 선언된 전압
   read noise → analog gain → ADC → black 기준 digital gain 순서를 고정한다.
   제안 noise plane은 `conversion_output_before_analog_gain`, 단위는 `V`와
   `V²`이다. 이 plane으로 환산했다고 선언한 전체 temporal read 항에 기존
   input-referred noise를 다시 더하지 않는다. 이 위치 선택 자체도 모델 가정이며
   실제 회로의 noise 발생 위치를 확인했다는 뜻이 아니다.

5. **DCG의 공통 전하와 경로 구분.** 두 경로는 기존처럼 공통 전하를 한 번
   샘플링하고 각자의 응답 곡선을 적용한다. 전압 read-noise 공분산에는 경로 순서,
   고정 gain, 온도 domain과 근거가 필요하다. 기존
   [`_calibration_path()`](../src/camera_platform/dcg_electrical.py#L70)의 상수 기울기
   보정이나 입력 전자 단위 공분산을 새 경로에 그대로 재사용하지 않는다.
   공통 shot noise를 두 독립 노출의 잡음으로 바꾸거나 평균으로 반감하지 않는다.

6. **RAW-only 역보정.** 새 calibration은 정확한 곡선·설정 hash, ADC/digital
   변환, dark와 노출, 경로별 검열 범위를 보존한다. 실제 integer RAW code가
   나타내는 ADC/digital 양자화 구간을 먼저 구하고, 그 구간을 전하 구간으로
   역변환한다. HDR·reference HDR·DCG가 같은 decoder를 사용하도록 한다.
   noiseless charge, sampled charge, simulation clipping mask를 역보정이나
   합성에 전달하지 않는다. RAW code에서 판단하는 검열과 simulation truth는
   기존처럼 별도 근거로 남긴다.

7. **Signed 저조도 경계.** Black 이하 RAW residual을 0으로 자르지 않는다.
   비음수 물리 전하 곡선과 read noise에 의한 signed 역보정 좌표를 구분하며,
   black 주변 역변환 continuation의 기울기·범위·가정을 계약에 명시한다.
   이 continuation은 음수 전자가 생성되었다는 물리 주장이나 임의의 모델 외삽을
   대신하지 않는다. 정의되지 않은 역변환, 포화·clipping 구간, 전부 무효인
   합성 결과는 mask와 NaN으로 남기며 preview만 표시 정책을 적용한다.

8. **분산과 양자화의 한계.** 비선형 역변환 뒤의 noise를 기존 상수 전자 공분산과
   동일하다고 취급하지 않는다. Local approximation을 사용한다면 적용 지점·기울기,
   knot 경계 통과 여부와 미포함 불확실성을 결과에 명시한다. Quantization interval은
   deterministic bound로 유지하고 독립 stochastic variance로 바꾸지 않는다.
   사용한 근사로 뒷받침할 수 없는 합성 confidence는 `unknown`으로 남긴다.

9. **영점·선형 회귀 시험.** Noise와 dark가 없는 0 electron 입력에서 선언된
   black/rounding 결과를 독립 scalar 계산으로 확인한다. 선형 곡선은 기존 모델의
   noiseless RAW와 일치해야 한다. 기존 v1의 전체 RAW·calibration·summary 회귀도
   유지한다. 비선형 예시는 `(0 e, 0 V) → (1000 e, 0.1 V) → (2000 e, 0.15 V)`로
   고정하고 500/1500 e의 순방향 ADC와 역변환 구간을 별도 scalar 계산으로 검산한다.

10. **HDR/DCG 수용 시험.** 같은 광원의 서로 다른 노출이 곡선의 다른 구간에
    놓인 fixture에서 상수-gain 역변환의 불일치를 드러내고, 새 역보정이 양자화
    허용 범위 안에서 같은 전자율을 복원하는지 확인한다. HCG 포화/LCG 유효,
    양쪽 포화, black 이하 signed residual, knot를 걸치는 RAW code, digital gain
    감쇠 뒤 숨은 ADC clipping을 포함한다. 합성 함수에 전달되는 입력을 검사하여
    실제 RAW와 고정 calibration 이외의 simulation oracle 사용을 실패시킨다.

11. **잘못된 lookup과 근거 거부.** 감소·평탄·중복 knot, NaN/무한값, 단위·shape
    불일치, 지원하지 않는 온도/gain, 부족한 전하 domain을 제출 전에 거부한다.
    V2 RAW에 v1 calibration을 연결하거나 다른 경로의 곡선·settings·source artifact를
    바꾸어 끼운 요청도 거부한다. 원문과 정규화 모델을 고정 artifact로 보존하고,
    누락·byte/hash 변조를 fresh read와 Worker 실행에서 검출한다.

12. **완결 경로와 자원.** Model/Program 등록 → 고정 Test → 실제 Worker 획득 →
    RAW/보정 배열 보존 → HDR/DCG 및 지원하는 RAW 재처리 → Case의 설계 판단까지
    같은 계약을 확인한다. Lookup은 bounded knot 수와 기존 frame/native pixel 예산을
    사용한다. 기존 [`dcg_runtime._parse()`](../src/camera_platform/dcg_runtime.py#L170)의
    정적 장면·고정 gain·획득 제약을 묵시적으로 완화하지 않는다.

13. **실측 검증과 남는 물리 범위.** 출처가 없는 곡선은 `estimated`, 수치해석에서
    얻은 곡선은 해당 계산 근거와 함께 `simulated`로 기록한다. 측정 곡선은 장비·단위,
    온도·gain·획득 설정, 원본 RAW/교정 근거, fitting과 평가에 사용한 구간을 연결한다.
    보류한 측정 구간의 residual과 paired dark/flat 근거가 있어야 해당 domain의
    적합성을 판단할 수 있다. 이 제안은 settling, charge transfer/reset 회로, CDS,
    trapping, blooming, 일반 TCAD 또는 실물 센서 전체 검증을 구현하지 않는다.
    모델 계산을 통과했다는 이유로 `physical_validation`을 완료로 바꾸지 않는다.

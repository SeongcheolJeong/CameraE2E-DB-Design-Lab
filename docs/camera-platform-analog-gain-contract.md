# 아날로그 gain 후보의 모델별 의미 계약 · 0.16.1.dev17

기존 `valid_domain.gain`은 숫자와 단위만 저장하며, 이 값이 광학 응답을 바꾸는지,
Sensor readout의 아날로그 제어인지, ISP 입력 코드의 적용 조건인지 구분하지 못한다.
범위가 겹친다는 사실만으로 새 gain의 실행 준비 상태를 보증할 수 없었다. 특히 합성
모델에서 Lens·Sensor·ISP의 범위를 동시에 0.4–1.0으로 넓히면, 이전 후보 검사에서는
0.4를 사전검사 적합으로 표시했다. 이 선언만으로는 gain 경로의 근거가 부족하다.

새 모델 버전은 `properties.analog_gain_applicability`에 다음의 명시적인 작성자 범위
선언을 넣을 수 있다. 모델의 `valid_domain.gain`은 계속 해당 후보를 포함해야 한다.

```json
{"schema":"camera_analog_gain_applicability_v1",
 "role":"sensor_model_readout_condition",
 "basis":"authored_model_scope"}
```

역할은 Lens의 `optical_response_independent`, Sensor의
`sensor_model_readout_condition`, ISP의 `raw_input_processing_condition`으로
고정된다. 잘못된 모델 역할, 단위, 유효 범위는 등록 시 거부한다. Lens의 역할은
**해당 광학 응답이 아날로그 readout gain 제어에 직접 의존하지 않는다는 작성자의
적용 범위 선언**이다. ISP의 역할은 고정된 처리 파라미터로 생성된 RAW 입력에 대한
적용 조건이지, ISP가 그 아날로그 gain을 적용한다는 뜻이 아니다. 어느 선언도
실측 정확도나 캘리브레이션의 독립 검증을 부여하지 않는다.

`POST /api/v1/lfm/candidate-plans`는 완료된 단일 선형 전기 AE run의 gain 후보를
`electrical_readout_analog_gain`으로 해석하고, Capture Program의 digital gain은
고정된 값으로 보고한다. 원래 gain과 다른 후보는 세 모델의 역할 선언이 모두 있어야
`eligible_for_new_frozen_run_preflight`가 될 수 있다. 선언이 하나라도 없으면 모델
숫자 범위와 PWM 정책이 맞아도 `unknown`과 미선언 모델 ID를 반환한다. 숫자 범위나
PWM 정책이 거절하면 `blocked`가 우선한다. 원래 gain을 유지하는 후보는 새 의미
선언을 요구하지 않아 기존 저장 사례를 읽을 수 있다.

개발 소스 `0.16.1.dev18`부터 실제 `electrical_control_v1` run 제출에도 같은 경계를
적용한다. Assembly 기준 gain과 다른 프레임이 하나라도 있으면, 해당 프레임의
Lens·Sensor·ISP 모델 역할 선언을 검사한다. 선언이 없는 모델 ID를 포함한
`analog_gain_model_role_undeclared` 사전검사 결과로 제출을 거부하며 Job을 만들지
않는다. Worker도 실행 전에 저장 원본으로 제출 계획을 다시 구성하므로 이 검사를
재적용한다. 기존 gain 그대로의 저장 사례와 별도의 gain 효과를 사용하지 않는 경로는
이 변경으로 재해석하지 않는다.

후보 API는 읽기 전용 계획 검사다. 전기 gain transfer가 실측되었는지, 아날로그
gain에서 read noise·포화·quantization이 어떻게 바뀌는지, 다른 온도·mode에서도
모델이 유효한지, HDR/LFM 목표가 달성되는지는 판단하지 않는다. 새 후보는 별도
모델 개정판·Control/Test·Worker 실행과 독립 reference 검증이 필요하다.

[원래 PWM 사례의 현재 판정](../reports/camera_platform/analog-gain-contract-v0161/original-pwm-plan.json) ·
[개발 근거](../reports/camera_platform/analog-gain-contract-v0161/evidence-summary.json) ·
[run 제출·Worker 종료 검증](../reports/camera_platform/analog-gain-submission-v0161/evidence-summary.json)

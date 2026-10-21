# 등록 RAW 기반 고급 ISP 잡음·질감 탐색

`source_bound_isp_optimization.run_recipe()`는 이미 등록한 비선형 RAW 네 건과
ISP v3 기준 모델을 받아, 고정된 denoise/sharpen 후보 탐색과 별도 취득 holdout을
한 번의 호출로 진행한다. 두 단계 모두 기존 `camera_isp_study_v2`의 계획,
불변 후보 모델, 소유 Worker 작업, 검증된 metric 및 export 근거를 사용한다.
실행 전 별도 Camera Platform Worker가 해당 state directory를 처리하고 있어야 한다.
`plan_recipe()`는 원본·보정·참조·취득 분리와 작업 예산을 읽기 전용으로 검사한다.

`pyisetcam.optimization.camerae2e_optimize_parameters()`는 FACA 장면을 다시
실행해 점수를 계산하며 공개 ISP 축은 `ip.demosaic_method`다. 보존된
charge RAW와 `camera_isp_profile_v3`의 denoise/sharpen 출력 계약과 다르다.
이 레시피는 FACA 점수나 후보를 RAW Study의 측정값으로 승격하지 않고,
기존 Camera Platform 고급 ISP 경로에 목적·제약과 두 단계 실행을 묶는다.

입력은 `camera_source_bound_isp_noise_detail_v1` JSON이다. `tuning_datasets`와
`holdout_datasets`에 각각 `flat`, `texture` 한 건씩 넣고, 각 항목은 기존
`ISPStudyDataset`의 `source_kind`, `source_id`, `selector`,
`acquisition_group_id`, `stationarity`, `metric_spec`, `reference_id`를 모두
명시한다. 네 원본은 서로 다른 등록 취득이어야 한다. 원본 RAW 해시,
개별 프레임 해시, scene·acquisition 계보의 중복은 두 Study를 제출하기 전에
차단한다. `texture`에는 후보 출력에서 만들지 않은 독립 reference ID가 필수다.
네 취득의 selector·readout·기하·정규화가 동일해야 하며 레시피가 제출 전에
검사한다. 보정·ISP 기준 모델의 적용성과 각 원본의 적합성은 기존 Study가
다시 검사한다.

탐색 축은 활성화된 `raw_denoise.strength`를 반드시 포함하고,
`linear_sharpen.amount`를 추가할 수 있다. 기준 프로파일을 포함한 최대
12개 후보를 각 데이터셋에 적용한다. 목적은 정지 flat의 **반복 프레임 간
pre-tone RGB 잔차 표준편차** (`temporal_residual_std_e`) 최소화 한 가지다.
단위 `electron_equivalent`는 고정 전하 정규화로 환산한 RGB 산술값이며
독립 실측 전자 잡음이나 시간적 장면 변화의 진실값이 아니다. 사용자가
미리 선언하는 다섯 제한은 flat 잡음 상한, 독립 reference 대비 texture
진폭 비율의 양쪽 경계, texture 절대 평균 편향 상한, holdout 잡음의
기준 대비 허용 증가량이다. 기준 없는 질감 보존이나 sharpness 단일 점수는
사용하지 않는다.

```python
from camera_platform import source_bound_isp_optimization as optimization

request = {
    "schema": optimization.SCHEMA,
    "name": "registered RAW noise/detail question",
    "baseline_isp_model_id": baseline_isp_model_id,
    "calibration_id": calibration_id,  # 등록된 불변 보정 ID 또는 null
    "axes": [
        {"parameter": "raw_denoise.strength", "values": [0.0, 0.4, 0.8]},
        {"parameter": "linear_sharpen.amount", "values": [0.0, 0.4]},
    ],
    "tuning_datasets": [tuning_flat, tuning_texture],
    "holdout_datasets": [holdout_flat, holdout_texture],
    "limits": {
        "flat_noise_max_e": 12.0,
        "texture_ratio_min": 0.85,
        "texture_ratio_max": 1.15,
        "texture_abs_bias_max_e": 20.0,
        "holdout_noise_noninferiority_e": 0.5,
    },
}
review = optimization.plan_recipe(service, request)
print(review["tuning_plan"]["plan_hash"], review["tuning_plan"]["job_count"])
receipt = optimization.run_recipe(service, request, timeout_seconds=300)
checked = optimization.get_receipt(service, receipt["id"])
print(checked["payload"]["assessment"])
```

위 수치는 예제 한계이며 카메라 제품의 합격 기준이 아니다. 실제 입력마다
시험 질문·독립 기준·단위를 근거로 값을 정해야 한다. 실행 후 완성된 tuning
후보 중 모든 제한을 만족하고 잡음 지표가 정의된 후보만 남긴다. 최소 잡음값,
다음 후보 ID 순으로 한 후보를 선택하고 기존 `isp_selections` 기록에 분석
해시와 선택 이유를 고정한다. 가능한 후보가 없으면 holdout을 실행하지 않고
`inconclusive` receipt를 저장한다. Worker 실패나 unknown 지표를 0으로
대체하거나 최적 후보로 선택하지 않는다.

Holdout Study는 tuning에 사용하지 않은 원본에 기준과 선택 후보만 적용한다.
새 순위를 만들지 않으며 선택 후보가 같은 제한을 만족하고 flat 잡음이
기준보다 미리 선언한 허용량 이상 나빠지지 않을 때 새 v2 receipt에는
`retrospective_criteria_pass`를 기록한다. 제한을 넘으면
`retrospective_criteria_fail`, 기준 자체를 선택하면
`retrospective_baseline_selected`, 누락·실패·unknown은 `inconclusive`다.
이 결과는 **등록 RAW의 제한 조건 통과**라는 뜻이며 제품 성능의 PASS/FAIL이
아니다. 과거 v1 receipt의 `pass` 문자열은 원본 상태로 재조회된다.
Receipt는 두 Study와 선택 record의 content hash,
분석 SHA, 결과를 연결한 JSON blob이다. `get_receipt()`는 원본 Study evidence를
다시 읽어 해시를 확인하지만 선택 규칙과 holdout 판정을 재계산하지는 않는다.
전체 원본·중간 배열·metric은 각 Study의
`isp_studies.export_manifest()`와 `export_bundle()`에서 받는다. 재호출은
같은 계획과 작업을 재사용한다. 지정한 timeout은 큐 작업을 삭제하지 않는다.

이 레시피는 요청 때 holdout의 네 원본 ID와 자료를 이미 받아 계획 단계에서
읽는다. 서로 다른 RAW 바이트와 취득 계보를 확인해도 사용자의 holdout 열람을
막는 접근 통제는 없으므로 **블라인드 holdout이 아니다**. 새 v2 receipt의
`source_evidence`는 실제 저장 source snapshot의 출처 분류와 reference
derivation 선언을 표시한다. `product_performance.available=false`,
`holdout_blinding_enforced=false` 및 blocker 코드를 함께 고정한다. 합성 RAW나
작성한 reference가 섞이면 해당 이유를 별도 blocker로 기록한다.

이 결과는 **등록 입력 범위에서의 계산 후보 판단**이다. 독립 reference도
출처 선언만으로 실측 독립성이나 정확도를 보장하지 않는다. 보정 uncertainty,
광학·픽셀 응답 정확도, 제조사 HW ISP 동등성, 제품 순위 및 release 승인은
성립하지 않으며 receipt에 `physical_validation: not_established`와
`product_ranking_allowed: false`를 남긴다.

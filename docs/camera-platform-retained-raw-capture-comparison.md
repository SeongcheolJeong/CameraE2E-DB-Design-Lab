# 저장된 Run RAW와 가져온 RAW의 제한적 비교

[`retained_raw_capture_comparison.py`](../src/camera_platform/retained_raw_capture_comparison.py)는
완료된 nonlinear retained Run의 **원본 정수 RAW 한 readout 경로**와
`camera_nonlinear_capture_v1`로 가져온 **원본 정수 RAW**를 같은 프레임·픽셀
인덱스에서 비교하는 선택형 Python 어댑터다. 캡처를 다시 계산하거나 전하량으로
역변환하지 않는다. 기존 Run/Capture 검증기를 통해 양쪽 원본 바이트와 등록
snapshot을 다시 읽는다. v1은 읽기 전용 HTTP/SDK 호출을 제공하며 Worker나
영구 비교 작업을 추가하지 않는다. HTTP는 로컬 플랫폼 토큰이 필요한
`POST /api/v1/retained-isp/raw-capture-comparisons`이고, SDK는
`compare_retained_raw_capture(run_id, capture_id, readout_id, roi)`다. POST는
ROI를 포함하는 JSON 본문을 받지만 등록 상태를 바꾸지 않는다.

```python
from camera_platform.retained_raw_capture_comparison import compare

result = compare(service, {
    "schema": "camera_retained_raw_capture_comparison_v1",
    "run_id": "run-...",
    "capture_id": "capture-...",
    "readout_id": "single",  # 또는 HCG, LCG
    "roi": {
        "row_start": 0, "row_end": 16,
        "column_start": 0, "column_end": 16,
    },
})
```

ROI는 수신된 RAW의 `[row, column]` 좌표이고 양 끝의 `end`는 제외한다. 최소
`2 × 2`이며 전체 프레임에 걸쳐 선택된 픽셀이 1,000,000개 이하여야 한다.
입력 Run과 Capture의 원본 로더는 각각의 자체 바이트·픽셀 상한을 적용한다.

비교는 같은 assembly ID와 snapshot, 같은 Test ID와 선언 조건, 똑같은
`response_type`·`bit_depth`·`cfa`·`black_level_dn`을 포함한 전체 RAW 형식,
native full-sensor 격자·crop origin·pixel pitch, 프레임 수와 배열 형상을
요구한다. `single`은 순차 캡처, `HCG/LCG`는 공유 전하 경로의 대응 경로와만
비교한다. 전체 선택 RAW나 선택 ROI가 시뮬레이션 배열의 복제이면 오류로 거부한다.
다른 경로·crop·형식·assembly를 조용히 보간하거나 재해석하지 않는다.

프레임마다 가져온 캡처의 `settings_evidence.status`가 `recorded_applied`이고
노출 시간·공통 gain·온도·선택 경로의 analog/digital gain이 Run과 일치해야
수치 지표를 낸다. `command_only`, `unknown` 또는 설정 불일치의 결과는
`comparison_status: INCONCLUSIVE`, `frame_metrics: null`과 구체적 `issues`다.
이 상태는 형식 오류와 구분된다. 입력 구조가 맞지 않거나 원본이 변조된 경우는
보고서를 만들지 않고 오류로 중단한다.

수치 지표가 있을 때 `comparison_status: DESCRIPTIVE_ONLY`다. 프레임별
시뮬레이션/캡처 평균 DN, `capture − simulated` 평균 DN, 픽셀별 MAE/RMSE DN,
black/white code rail 비율, native 원점의 RGGB 각 site별 평균 DN 차이를
기록한다. 결과에는 양쪽 선택 배열·ROI의 정규화 픽셀 해시와 캡처의 원본
artifact 해시, Run/Capture/Test 신원도 있다. 동일 인덱스의 오차 크기는 장면
조도, 실제 장비와 모델의 동등성 또는 정확도를 증명하지 않는다.

가져온 메타데이터가 `source_kind: measured`라 하더라도 그 선언은 외부 인증이나
독립적인 실측 진실이 아니다. 이 어댑터는 광학 장면의 동일성, 장비와 모델의
물리적 결합, timestamp 정확도, temporal/HDR/LFM/ISP optimizer 성능,
측정 불확실도 또는 접근 통제된 blind holdout을 검증하지 않는다. 결과는 항상
`physical_validation: not_established`이며 실물 PASS/FAIL을 만들지 않는다.
양성 회귀 fixture 역시 `source_kind: simulated`인 저작 데이터를 사용하며
측정 건수로 세지 않는다. 실측 성능 주장은 별도 원본 측정 프로토콜과
독립적인 기준 데이터, 계측기 보정 및 불확실도 평가가 필요하다.

# 작성된 시간 참조를 이용한 ISP 후보 비교

개발 소스 `0.16.1.dev23`의 `camera_retained_code_comparison_v2`는 저장된 원본 RAW 한 경로와 고정 색 처리 설정에, 별도로 작성한 시간별 RGB 코드 참조를 묶는다. v1의 동일 RAW·무필터 기준 지표는 유지하고, v2에서만 참조 대비 신호 변화 지표와 제약을 추가한다. 참조는 ISP 처리 함수의 입력이 아니며 후보 출력의 평가에만 쓰인다.

1. `GET /api/v1/retained-isp/temporal-references/template?source_id=…&readout_id=single&isp_model_id=…`에서 원본 RAW, Test, frame ID, row 전하 전달 시각, 수신 ROI와 고정 demosaic/WB/CCM의 결합 정보를 받는다. 독립 생성 방법·버전·라이선스·불확실성 및 실제 시간 변화 원인을 메타데이터에 작성한다. 기본 `replace-with-…` 출처 문구를 그대로 둔 등록은 거부한다.
2. 원본 프레임×native 높이×너비×RGB 3 채널의 C-order `float64` NPY (`[F,H,W,3]`)를 별도 생성한다. 값은 고정 CCM 뒤, tone과 후보 detail 앞의 정규화 코드 공간이며 ROI 안의 참조는 시간 변화가 있어야 한다. `POST /api/v1/retained-isp/temporal-references/import`에 `metadata` JSON과 `image` NPY를 multipart로 전송한다. 원본 바이트, 메타데이터, 출처 및 Test 결합은 저장 뒤 재조회 때 다시 확인한다.
3. `GET /api/v1/retained-isp/temporal-references/{id}/test-template`의 응답을 검토해 기존 `POST /api/v1/tests`로 불변 Test를 등록한다. Test의 tolerance를 선택하고, ROI·시간축·원본 Test·참조 해시를 유지한다.
4. 기존 후보 비교 v1 요청에 `schema: camera_retained_code_comparison_v2`, `temporal_test_id`, 명시적 `temporal_objectives`, `temporal_constraints`를 추가한다. 신호 변화 RMS 오차를 목적에, 절대 코드 편향·진폭 오차·신호 변화 RMS 오차를 각각 제약에 포함해야 한다. `/api/v1/retained-isp/comparisons/plan`이 참조와 자원 상한을 확인하고, 검증된 계획 해시로 제출한다. 결과·bundle에는 참조 ID/해시와 후보별 판정이 남는다. ISP 화면의 “참조 Test를 묶은 v2 초안”은 기존 v1 초안을 변환하며 제약 수치는 연구용 예시다.

참조 `ccm_code_rgb_frames`와 후보의 최종 `pre_tone_code_rgb_frames`를 비교하므로 후보 detail의 영향을 포함한다. 신호 진폭 보존 비율은 진단치이고, 그 비율만 최대화하면 과증폭을 선호할 수 있어 `signal_amplitude_error=|1−보존 비율|`을 최소화한다. 일정한 밝기 편향은 절대 코드 오차가, 시간 변화 불일치는 신호 변화 RMS 오차가 잡는다. step의 `onset_delay_ns`는 참조·후보의 실제 row 전달 시각에서 threshold를 넘은 시점의 **절대 차이**로, 조기·지연을 모두 벌점으로 준다. 정착 지연, 잔상, 안정 구간의 2프레임 주기 진폭 및 과거 표본의 추가 나이도 기록한다. 선언 step의 자극 전·안정 구간과 참조 변화 window를 검사하고, 수치상 불안정한 최소 변화량(<`1e-6`)은 입력에서 거부한다. 클리핑, 부족한 시간 변화나 유효 표본, 불확실한 ROI는 해당 지표를 `unknown`으로 두고 후보를 Pareto 선정에서 제외한다. 원래 같은 RAW 기준 지표는 참값이 아니다.

현재 허용 참조는 **작성자가 독립이라고 선언한 합성 소스**다. 생성 독립성을 인증하거나 실물 카메라로 검증하지 않았고, 참조 생성 모델의 물리 오차 상한도 알 수 없다. `compatible` 또는 합성 Test 통과는 센서 성능 보증과 다르다. v2의 Pareto는 평가 가능한 유한 후보에만 적용되며 자동 출시 후보를 고르지 않는다. dev23 당시에는 등록된 HDR spectral import가 `capture_crop`을 거부했다. dev24는 등록형 HDR 부분 source→receiver ROI를 지원하지만, 이 변경이 ISP 시간 참조의 독립성이나 실측 정확도를 높이지는 않는다. 지원 범위와 남은 종단 검증은 [등록 ROI 계약](camera-platform-registered-temporal-roi.md)을 따른다.

구현: `temporal_signal_contracts.py`, `temporal_signal_reference.py`, `temporal_signal_metrics.py`, `retained_isp_comparison.py`, `static/retained-isp.js`.

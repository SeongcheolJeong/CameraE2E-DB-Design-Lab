# 선택을 잠근 뒤 취득하는 ISP 홀드아웃

[`retained_isp_prospective_optimizer.py`](../src/camera_platform/retained_isp_prospective_optimizer.py)와
[`retained_isp_prospective_workflow.py`](../src/camera_platform/retained_isp_prospective_workflow.py)는
기존 [관측 기반 ISP 최적화 v1](camera-platform-observable-isp-optimizer.md)의
사후 평가 한계를 줄이는 **별도 선택형 v1**이다. 개발 선택 요청에는 개발 Joint
Study v3의 ID만 들어간다. 홀드아웃 ID·결과·배열은 선택 계획, 등록 작업, 선택
artifact 어디에도 들어가지 않는다. 선택 작업이 성공해 후보 ID와 ISP 프로파일
해시가 불변 artifact로 고정된 뒤에만 새로운 홀드아웃 Joint Study를 지정할 수
있다. 기존 사후 평가 API와 artifact는 그대로 유지된다.

순서는 다음과 같다.

1. 개발용 Joint Study v3를 완료한다. `POST /api/v1/retained-isp/prospective-optimizer/selections/plan`에
   `camera_retained_isp_prospective_optimizer_v1`을 보낸 뒤 받은 `plan_hash`로
   `/selections`를 제출한다. `baseline_candidate_id`, noise·시간·색차의 목적과
   시간·색차·RGB 변화의 상한을 미리 선언한다. Worker는 개발 원본 RAW와
   저장된 후보 ISP 배열을 다시 검증해 선택을 고정한다.
2. **선택 작업 완료 이후** 홀드아웃의 각 원본 RAW Run과 시간 참조를 등록하고
   Joint Study v3를 완료한다. 같은 Assembly·ISP 모델·후보 grid·readout·RAW
   형식·native 격자·ROI를 써야 한다. 사전에 정의된 Assembly, Test, 모델,
   조건 계약은 재사용할 수 있다.
3. `/holdouts/plan`에 `selection_job_id`와 `holdout_joint_study_id`를 보낸다.
   홀드아웃 Joint 작업, **모든 원본 홀드아웃 RAW Run**, 시간 참조 record의
   registry `created_at`이 선택 작업의 성공 `updated_at`보다 엄격히 나중이어야
   한다. 이는 데이터 등록 순서의 증거이지 카메라 시계의 정확도 증거가 아니다.
   개발·홀드아웃 원본 Run ID, 선택 배열 해시, Run snapshot 해시가 겹치면
   거부한다. 양쪽 원본 RAW를 재검증하고 정규화한 **프레임별 픽셀 해시**도
   대조하므로 NPY 재포장이나 일부 프레임 복제로 홀드아웃을 만들 수 없다.
   시간 참조 record는 새로 등록해야 하나, 같은 사전 정의 target의 진실 배열이
   같은 숫자를 갖는다는 이유만으로 독립 취득을 부정하지 않는다.
4. `/holdouts`에 계획 해시를 제출한다. Worker는 전체 홀드아웃 Joint 후보를
   냉간 검증하고, 잠근 후보와 기준 후보 최대 두 개의 관측 지표만 계산한다.
   홀드아웃 결과로 후보를 다시 고르지 않는다. 작업 조회·export는 저장된 JSON
   artifact와 원본 배열을 다시 검증한다. SDK는
   `plan_prospective_isp_selection` → `submit_prospective_isp_selection`,
   `plan_prospective_isp_holdout` → `submit_prospective_isp_holdout`과 조회·export를
   제공한다.

결과의 `consistency_status`는 `consistent`, `inconsistent`, `inconclusive`다.
이것은 선언한 후보의 **기준 ISP 대비 상대 noise·프레임 변화·display 차이**가
선언 상한과 점수를 만족하는지에 한정된다. 실제 색·움직임·HDR/LFM·물리
카메라 성능에 대한 PASS/FAIL이 아니다. 미확정 지표는 `inconclusive`로 둔다.
`physical_validation`은 항상 `not_established`다.

선택 요청에서 네 번째 선택 지표 `display_temporal_change_rms_delta`를 목적 또는
`<=` 제약으로 미리 선언할 수 있다. 단위는 `normalized_display_rgb`다. 고정한
ROI의 저장 display RGB에서 프레임 간 차이를 구하고, 후보의 차이와 기준 ISP의
차이 간 RMS를 계산한다. 후보가 톤 처리 전 시간 변화를 남겨도 표시 결과를
평탄화하면 이 값이 커져 선언한 상한으로 거부할 수 있다. 기준 display에
분해 가능한 프레임 변화가 없거나 기존 RAW/시간 입력 근거가 미확정이면 이
지표는 `unknown`이다. 이 비교는 **기준 ISP 출력의 시간 형태를 보존하는지**만
검사하며 장면의 실제 움직임이나 HDR/LFM 품질을 평가하지 않는다. 이 지표를
선택하지 않은 기존 요청·v1 artifact는 기존 결과 계약을 유지한다.

공유 로컬 호스트의 사용자는 자료가 생성된 뒤 다른 API나 파일로 홀드아웃을
열람할 수 있다. 이 경로는 선택 전 홀드아웃 부재와 등록 시간, 원본 출처 분리를
증명하지만 **접근 통제된 블라인드 홀드아웃은 제공하지 않는다**.
`holdout_blinding_enforced: false`가 결과에 남는다. 실제 블라인드 검증에는
권한이 분리된 취득·보관·해제 절차가 추가로 필요하다. Joint Study의 시간 참조와
현재 테스트 자료는 저작 또는 시뮬레이션일 수 있으며, 별도 실측·계측기 보정·
불확실도 없이 물리 정확도를 주장할 수 없다.

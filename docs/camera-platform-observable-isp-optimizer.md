# 저장된 RAW·ISP 출력으로 하는 두 단계 ISP 최적화

[`retained_isp_observable_optimizer.py`](../src/camera_platform/retained_isp_observable_optimizer.py)는
완료된 [Joint ISP Study v3](camera-platform-joint-isp-study.md) 두 건을
**개발 입력**과 **출처가 분리된 사후 평가 입력**으로 묶는다. 개발 단계에서는
개발 후보의 저장된 정수 RAW/ISP 결과만 사용해 후보를 고른다. 선택 결과를 별도
불변 JSON artifact와 Worker 작업으로 고정한 뒤 사후 평가의 전체 후보 배열을
Joint Study 검증기로 재확인한다. 그중 선택한 후보와 선언된 기준 후보의 지표만
계산하며 다시 순위를 매기지 않는다.

개발·미사용 평가의 후보 grid, ISP 모델 snapshot, RAW 형식, 렌즈·픽셀·readout
계약, assembly ID, 선택 readout, native 격자 및 ROI가 같아야 한다. 양쪽의 원본 run ID, 선택 RAW 바이트 해시,
원본 run snapshot 해시가 하나라도 겹치면 미사용 평가를 거부한다. 이는 동일
취득물의 다른 이름으로 holdout을 만드는 일을 막는다. 독립적인 물리 장면이나
장비 조건을 보장하는 검사는 아니며, 같은 Test 아래의 새 seed 취득은 허용한다.

지표의 기준면과 의미는 다음과 같다.

| 지표 | 단위 | 계산·해석 |
| --- | --- | --- |
| `stationary_repeat_residual_std` | `normalized_code_rgb` | Joint Study에서 동일 정적 장면을 서로 다른 원본 noise seed로 취득한 반복 간 pre-tone RGB 표본 분산. 해당 Study의 냉간 재검증을 통과한 값만 사용한다. |
| `temporal_change_rms_delta` | `normalized_code_rgb` | 같은 동적 RAW의 후보와 기준 ISP가 출력한 pre-tone RGB의 프레임 간 변화량 차이 RMS. 절대 시간 신호의 참값 오차가 아니다. |
| `display_chroma_rms_delta` | `normalized_display_rgb` | 최종 display RGB에서 `R−G`, `B−G` 두 색차가 기준 ISP와 달라진 정도의 RMS. 측색 정확도나 ΔE가 아니다. |
| `display_rgb_mae_delta` | `normalized_display_rgb` | 최종 display RGB 전체의 기준 ISP 대비 절대 차이 평균. 밝기·tone 이탈 제한에 사용한다. |

시간 비교 ROI의 원본 정수 RAW에 black/white code rail이 하나라도 있거나 프레임 간
원본 RAW 변화가 0이면 시간 지표를 미확정으로 둔다. 기준 ISP 또는 해당 후보의
pre-tone 코드가 `[0, 1]` 밖이거나 기준 출력의 변화 RMS가
`1e-6 normalized_code_rgb` 미만이어도 미확정이다. 이 검사는 비교 가능한
**관측 변화**의 최소 조건일 뿐이며 실제
움직임·가림·PWM 신호의 진실성은 입증하지 않는다.

반복 잡음·시간 변화·색차는 필수 최소화 목적이며, 각 목적에 사전 선언한 양의
가중치와 scale을 적용해 `Σ weight × metric / scale`을 계산한다. 같은 점수는
후보 ID로 결정한다. 시간 변화·색차·최종 RGB 차이의 명시적인 `≤` 제약은
필수다. 제약 실패나 정적 잡음 미확정 후보는 선택 대상에서 제외한다. 모든 후보가
제외되면 선택 ID를 `null`로 저장하고 holdout 출력은 읽지 않는다. Pareto
여부와 탈락 이유도 개발 결과에 남긴다. 가중치와 scale은 사용자가 목적에 맞게
정해야 하며, 저장 지표만으로 통계적 우월성이나 실물 성능을 자동 선언하지 않는다.

HTTP/SDK 순서는 아래와 같다.

1. `POST /api/v1/retained-isp/observable-optimizer/selections/plan`에
   `camera_retained_isp_observable_optimizer_v1` 명세를 보낸다. 이어서
   `/selections`에 동일 명세와 `plan_hash`를 제출한다. Worker가 고정된 개발
   선택을 다시 검증하고 `isp-observable-selection.json`을 저장한다.
2. 선택 작업이 완료된 뒤 `/holdouts/plan`에 `selection_job_id`를 보내고,
   `/holdouts`에 계획 해시를 제출한다. Worker는 고정 선택 artifact를 다시 읽고,
   기존 Joint Study의 **전체 후보 출력 배열을 냉간 검증**한 다음 기준·선택
   후보 두 프로파일만 점수화해 `isp-observable-holdout.json`을
   저장한다. 결과의 `holdout_status`는 선택 후보가 선언 제약을 지키고 동일
   가중치의 holdout 점수가 기준 ISP보다 악화되지 않았는지에 대한
   `pass`/`fail`/`inconclusive`다. 점수 또는 제약 지표가 미확정이면
   `inconclusive`로 둔다. 이 상태는 물리 정확도 판정이 아니다.
3. 두 작업의 조회·`/export`는 원본 Joint 작업과 content-addressed JSON
   artifact의 경계를 다시 확인한다. 원본 RAW 및 RGB NPY 전체는 각 Joint
   Study의 기존 bundle에서 획득한다. SDK는
   `plan_observable_isp_selection`, `submit_observable_isp_selection`,
   `plan_observable_isp_holdout`, `submit_observable_isp_holdout` 및 조회·내보내기를
   제공한다.

두 Joint Study는 선택 작업보다 먼저 완료되어 있어, 별도 API에서 사후 평가의
후보·시간 참조 지표를 사람이 미리 열람할 수 있다. 이 기능은 **출처 분리와
선택 후 재선택 금지**를 강제하지만, 접근 통제된 블라인드 또는 전향적
holdout을 제공하지 않는다. 보고서에 `holdout_blinding_enforced: false`와
`source_disjoint_retrospective_assessment`를 명시한다. 블라인드 검증 주장은
선택을 먼저 잠그고 이후 별도 권한으로 취득·해제하는 프로토콜이 있어야 한다.

Joint Study 자체는 등록된 시간 참조 Test로 그 **별도 보고 지표**를 냉간
검증한다. 따라서 이 최적화는 truth-qualified Joint Study v3를 필요로 하며
원본 RAW만으로 단독 실행되는 경로가 아니다. 최적화의 목적 함수와 선택은
참조 배열, Joint Pareto 순위 및 `signal_change_rms_error`를 읽지 않는다.
이 설계의 시간 지표는 기준 ISP에
대한 상대 변화 보존에 한정된다. 실제 움직임·가림·flicker에서 무엇이 참 신호인지
확인하려면 독립 실측·불확실도·조건별 holdout이 필요하다. 결과는
`physical_validation: not_established`로 남는다.

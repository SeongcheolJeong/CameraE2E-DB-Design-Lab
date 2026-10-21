# Retained ISP의 정지 잡음·시간 신호 공동 비교

이 기능은 **같은 ISP 프로파일 후보**를 서로 다른 두 입력군에 적용한다. 첫째는
같은 정적 Test를 독립 seed로 다시 취득한 4–16개 원본 정수 RAW이고, 둘째는
시간 변화가 있는 원본 정수 RAW와 별도 등록 Test에 결속된 시간 참조다.
후보별로 정지 반복의 동일 프레임·동일 픽셀·RGB 코드 표본 분산과 시간 참조 대비
실제 신호 변화 오차를 함께 계산한다. 한 시퀀스의 프레임간 분산을 잡음으로
간주하지 않는다. 그 값에는 픽셀의 이전 전하, PWM 및 시간 신호가 섞일 수 있다.

정지 잡음 지표 `stationary_repeat_residual_std`는 고정 ROI의 같은 프레임과
픽셀에서 반복 run 사이의 표본 분산을 구한 뒤 프레임·픽셀·RGB 성분 평균의
제곱근이다. 단위는 `normalized_code_rgb`이다. 정지 scene의 모든 spectral
image knot가 같은 원본 해시이고, 두 번째 광원 성분도 정적이며, 광원 파형이
없거나 일정하고, Test·조립·readout·조건·행 시각이 일치하고, 잡음이 켜진
서로 다른 seed의 취득임을 확인한다. 원본 RAW의 rail 또는 처리 코드 clipping이
고정 ROI에 있거나 이 조건을 입증할 수 없으면 지표를 `unknown`으로 둔다.

시간 참조는 후보 처리기에 입력하지 않는다. 별도 Test가 원본 동적 RAW,
선택 readout, ISP 모델, ROI와 행 시각을 고정한 뒤 평가할 때만 사용한다.
`signal_change_rms_error`와 정지 반복 잡음을 필수 최소화 목적으로 두고,
절대 코드 편차와 신호 진폭·변화 오차의 명시적 상한을 적용한다. 실패하거나
필수 지표가 미확정인 후보는 Pareto 후보에서 제외하며 자동 선택·모델 승격은
하지 않는다. 후보별 실제 처리 프로파일, pre-tone RGB·temporal gate의
원본 배열과 해시, gate 활성 수를 근거로 남긴다.

현재 검증 범위는 **합성 retained-code 원본과 작성한 독립 시간 참조를 이용한
소프트웨어 계산**이다. 반복 seed의 독립성은 시뮬레이터 계약에 근거하고,
실물 카메라의 잡음·ghost·지연 개선이나 참조 계측의 독립성을 인증하지 않는다.
실측 결과에 적용하려면 원본 취득·장비·시계 정합, 정적 참조, 계측 불확실도와
별도 조건 검증이 먼저 필요하다.

계산 엔진은 [`retained_isp_joint_study.py`](../src/camera_platform/retained_isp_joint_study.py),
등록·실행·내보내기 경계는
[`retained_isp_joint_workflow.py`](../src/camera_platform/retained_isp_joint_workflow.py)다.
`/api/v1/retained-isp/joint-studies/plan`으로 등록된 4–16개 정적 run ID,
동적 run ID, ISP 모델 ID 및 시간 Test ID를 계획한다. 제출은 계획 해시를
요구하며 Worker와 조회·내보내기는 원본/참조 artifact를 다시 확인한다.
Python SDK는 `plan_retained_isp_joint_study`와
`submit_retained_isp_joint_study`를 제공한다. 저장 결과에는 후보별 실제 처리
해시·지표·Pareto와 원본 artifact bundle이 포함된다. 새 `result_v3`는 각
후보·정적 취득·동적 취득의 float64 pre-tone RGB, boolean temporal gate와
최종 float64 display RGB를 별도 NPY로 보관한다. 계획 단계에서 세 배열의
비압축 크기와 ZIP 별칭까지
예산에 넣으며 256 MiB 경계를 넘는 계획은 제출 전에 거부한다. 조회와
내보내기는 NPY의 타입·형상·내용, 원본 RAW·등록된 시간 참조를 재확인하고
저장된 배열에서 정지 잡음·시간 지표·Pareto를 다시 계산한다. ISP 처리기를
재실행하지 않아도 **저장된 출력에 대해 보고한 수치**를 검증할 수 있다.
최종 display RGB도 저장된 pre-tone 값과 고정 tone 설정의 clipping·
linear/sRGB 전달을 재계산해 픽셀별로 대조한다. 따라서 후보가 실제로 사용자에게
보여줄 코드는 번들에서 읽을 수 있다. 정지 잡음과 시간 신호 지표의 기준면은
계속 **pre-tone code RGB**이며 최종 표시 색공간의 측정 품질 지표가 아니다.
이는 저장된 배열이 실제 처리기로 생성됐음을 별도의 구현으로 독립 증명하는
절차는 아니다. 기존 `result_v2`/`budget_v2`의 두 배열은 계속 냉간 검증하지만
최종 RGB는 소급 보관되지 않는다. `result_v1`/`budget_v1`도 계속 읽을 수 있으나
배열을 소급 보관할 수 없어 구조·해시·순위 검증에 한정된다.

등록형 종단 시험은 실제 Mitsuba 3D 렌더를 발행해 두 knot가 같은 불변
분광 이미지를 참조하는 정적 장면을 만든 뒤, 네 번의 별도 잡음 취득과 동적
Test를 Worker까지 실행했다. 이 장면에는 fixture `pattern=static` 표지가
없지만 이미지 바이트가 같아 정지 잡음 지표가 유효했다. 발행 기록에 같은
`spectral-image.npy` 바이트 설명자가 두 번 나타날 때는 하나의 별칭으로
판정하며, SHA나 크기가 다르면 정지 근거로 인정하지 않는다. 동적 참조는
작성한 합성값이므로 실제 3D 장면의 독립 계측 진실값으로 승격하지 않는다.

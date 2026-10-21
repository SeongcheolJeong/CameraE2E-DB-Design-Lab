# PWM 노출 공간 진단 · 0.16.1.dev13

`camera_electrical_exposure_policy_v2`의 정수 PWM 주기와 노출 격자를 함께 검사한다. 이번 확장은 실제 적용 설정과 원본 정수 RAW의 선택 CFA 사이트 평균을 사용해 **정책상 허용되는 노출 공간**과 **관측 프레임의 목표 진입**을 구분한다. `camera_pwm_exposure_space_diagnosis_v1`은 결과의 `electrical_control.target_outcome.exposure_space_diagnosis`에 저장된다. 같은 정보를 브라우저의 전기 AE 결과에도 표시한다.

허용 노출이 하나이고 관측 프레임이 목표 밖이면 `only_permitted_exposure_missed_in_observed_frames`다. 이것은 **고정된 정책 안에서 노출만 바꿀 다른 설정이 없다**는 구조적 결론이다. 목표가 물리적으로 불가능하거나 Gain/HDR 경로도 실패한다는 결론은 아니다. 허용 노출이 여럿인데 목표가 관측되지 않으면 `target_unobserved_with_exposure_alternatives`다. 모든 설정이 유한 시퀀스에서 한 번씩 관측돼도 이력·시점·PWM 위상·잡음이 서로 달라 전체 목표 가능성은 미확립으로 남는다. 목표에 들어온 프레임이 있어도 별도 반복·독립 실측이 필요하다.

진단은 다음에 구분할 실험을 명시한다. 단일 노출 사례에서는 **같은 PWM 주기 아래 별도 Gain 또는 HDR 경로**를 비교하고, 다중 노출 사례에서는 **조건을 맞춘 고정 노출 격자 스크리닝**을 수행한다. 구성·장면·파형/위상·RAW 측광·프레임 시간/clear·온도를 고정하고 실제 적용 설정, 원본 RAW, rail, 잡음/잔류 상태, 위상/시드 범위를 기록해야 한다. 현재 기능은 이 실험을 자동 실행하거나 최적 조합을 선택하지 않는다.

작성한 8×8·4프레임 trap/PWM 사례의 허용 노출은 **8 ms 하나**다. 실제 적용은 8/8/8/8 ms였고 RAW 목표 200–300 DN 대비 관측은 571.6875/606.75/584.21875/608.875 DN으로 **0/4**였다. 기존 코드와 동일한 원본 RAW 및 trap 전하를 다시 확인했다. 관련 Python **422개**, Node UI **311개**, 실제 Worker **2건**, HTTP 최초 **150개**·재시작 **152개** 검사가 통과했다. 브라우저와 재시작 화면에서 진단·다음 실험을 확인했고 오류는 0건이었다. QA 서버/Worker는 모두 종료했고 기본 앱의 기존 DB 행·PID는 유지됐다.

이 사례는 밝기 목표를 달성하지 못했다. Gain/HDR의 실제 후보 실행, 주기와 밝기 상충의 다목적 탐색, PWM 위상·온도·움직임·잡음 반복, 독립 물리 LFM 측정은 남아 있다. 실측 **0건**, 총 예측 불확실도 `unknown`, 물리 정확도와 LFM 성능 `not_established`다. 개발 소스는 기본 설치본 v0.15.0에 배포하지 않았다.

[근거 요약](../reports/camera_platform/lfm-tradeoff-v0161/evidence-summary.json) · [최종 검토](../reports/camera_platform/lfm-tradeoff-v0161/final-consistency-review.json)

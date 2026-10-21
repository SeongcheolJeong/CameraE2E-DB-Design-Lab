# 전달 RAW를 관측하는 전기 모델 노출 AE

`0.16.1.dev3` 개발 소스에는 **단일 전기 readout의 전달 RAW를 관측해 다음 노출을
조절하는 폐루프 경로**가 있다. 실제 적용 노출로 광학·PWM 적분과 PD 전하 이동을
다시 계산하고, 명령·적용 시각·RAW·표시 RGB를 함께 보존한다. 설치본이나 기본
작업실로의 인도 여부는 [개발 인도 기록](../reports/camera_platform/development-handoff.json)을
따른다. 이 문서는 실행 계약을 설명하며 실제 카메라 정확도나 제어 안정성의 인증이 아니다.

원문 **Asset Registry, Model Factory, Camera Composer, Simulation Runtime,
Validation Center, Experiment & Optimization, Agent & Release Control**의 일곱 모듈과
**Quick Estimate, Response-DB Simulation, Surrogate Prediction,
Detailed Characterization**의 네 실행 방식을 유지한다. 이번 변경은 일반 광학·픽셀·전기
모델과 HDR/LFM·고급 ISP·최적화라는 전체 목표 중 전기 모델 노출 피드백을 연결한 범위다.

## 고정 입력과 실제 적용값

| 경계 | 식별자·역할 |
| --- | --- |
| Runtime | `electrical_control_v1`, 작업 종류 `camera_simulation` |
| Capture Program | `camera_electrical_control_capture_program_v1` |
| 순수 정책 | `camera_electrical_exposure_policy_v1` |
| Test stimulus | `electrical_control_test_v1` |
| 계획 계약 | `camera_electrical_control_plan_v1` |
| 실제 획득 계약 | `camera_electrical_control_realized_samples_v1` |
| 결과·추적 | `camera_electrical_control_result_v1`, `camera_electrical_control_trace_v1` |
| 관측 지표 | `raw.electrical_control_delivered_channel_mean_dn_sequence`, 단위 `DN` |

Capture Program은 기존 잔류 PD 프로그램의 `electrical_model`, `retained_charge_model`,
`waveform`, `event_contract`, `pd_clear_before_exposure`, `preview`에
`electrical_control_policy`를 추가한다. 현재는 `camera_electrical_readout_v1`의 선형
단일 경로와 `camera_electrical_readout_v2`의 비선형 단일 경로를 지원한다.
관측 `readout_id`는 `single`이며 실행 중 바뀌지 않는다.

기존 `recorded_control_sequence_v1`은 이 경로에서 **시작 시각·shutter mode·초기 설정의
고정 틀**이다. 모든 프레임의 초기 exposure/gain/ISP와 선택적인 WB는 같아야 하며,
초기 `command_id`는 `null`, `applied_at_ns`는 해당 latch와 같아야 한다. 초기 노출은
정책 grid 위에 있어야 한다. 미리 기록한 프레임별 가변 노출이나 미래 명령·전송 완료
기록을 폐루프 입력으로 혼합하면 거부한다.

입력 Test의 `frame_conditions`는 초기 설정을 실제 획득값으로 선언하지 않도록 `null`이다.
출력 `summary.control_sequence`, `summary.frame_conditions`, `summary.frames`는 실제
적용값을 사용한다. `planning_contract`는 원래 계획을 보존하고, prediction의
`sample_geometry`는 실현된 control sequence와 행별 사건을 연결한다. 초기 계획의 노출로
후속 프레임의 조건을 덮어쓰지 않는다.

## RAW 관측과 순수 노출 정책

정책의 전체 필드는 다음과 같다. Clock과 프레임 시작 시각은 원래 sequence에만 있으며
정책에 중복 선언하지 않는다.

| 필드 | 계약 |
| --- | --- |
| `enabled` | 정확한 boolean. `false`이면 관측은 남기고 초기 노출을 유지 |
| `exposure_grid_ns` | 양의 정수 `minimum`, `maximum`, `step`. 최대값은 최소값에 정렬된 grid 위 |
| `target_black_relative_dn` | 양의 `minimum`, `maximum`, 양 끝을 포함하는 DN 목표 구간 |
| `max_step_factor` | `1 < factor <= 16`. 양자화된 실제 제안에도 증가·감소 배율 한도 적용 |
| `minimum_delay_frames` | `1..16`, 같은 프레임 피드백 금지 |
| `maximum_delay_frames` | 최소 지연 이상, 최대 16. 마지막 허용 프레임까지 적용 가능 |
| `statistics_roi` | `reference: native_sensor`, 절대 `origin`, 짝수 `shape` |
| `meter` | `native_site_mean_black_relative_dn_v1`, 중복 없는 `R/Gr/Gb/B` 사이트 목록 |
| `latency` | `kind: declared_simulation` 및 아래 여섯 개 정수 ns 지연 |

관측 입력은 full-native RGGB의 정수 RAW 한 프레임이다. 3차원 paired RAW, float RAW,
지원하지 않는 CFA나 읽기 경로를 받지 않는다. ROI 안에서 선택한 native 사이트의 모든
코드를 같은 표본 가중치로 집계한다. Origin이 홀수여도 full-native 좌표의 CFA 위상을
유지한다. 기본 사이트는 `Gr`, `Gb`이며 R과 B가 자동으로 포함되지는 않는다.

```text
value_dn = sum(selected_integer_codes) / sample_count - declared_black_level_dn
```

Black 아래의 음수값과 low/high digital rail 코드를 제외하거나 0으로 잘라내지 않는다.
`sum_codes_dn`, `sample_count`, 선택·제외 사이트, 네 사이트 평균, digital rail 비율을
기록한다. 소수 black level 근처의 상쇄를 포함해 관측과 정책은 동일한 명시적 Decimal
정밀도를 사용한다. Rail 비율은 최종 코드 범위의 관측이며 PD full-well 포화율이 아니다.

목표 구간 안에서는 유지한다. 양의 관측에는 목표 구간 중심/관측값의 비율을 사용하는
제한된 비례 피드백을 적용하고, 0 이하에서는 최대 허용 배율로 노출 증가를 제안한다.
노출 범위와 변화율을 제한한 뒤 정수 grid로 양자화하며 정확한 반 간격은 위쪽으로
반올림한다. 거친 grid 때문에 허용되는 새 값이 없으면 `actuator_grid_hold`로 남는다.

정책은 gain, WB, ISP, PD clear를 명령할 수 없다. Exposure로 관측 DN을 나누지 않고,
비선형 charge-voltage 곡선을 역산하거나 dark current·잔류 전하를 빼지 않는다.
목표는 작성자가 선언한 **전달 코드 구간**이며 현재 프레임 광전하·장면 휘도·적정 노출의
추정값이 아니다. 광학의 미래 입력과 전하 ledger는 정책 함수에 전달하지 않는다.

## 시간과 명령 적용

원래 상대 ns clock을 사용하며 정수와 boolean을 구분한다. Timestamp는 JSON 숫자로
정확히 표현되는 `±(2**53-1)` 안에 있고 전체 상대 span도 그 한도를 넘지 않아야 한다.
Exposure는 최대 3,600초의 정수 ns다. Rolling shutter의 latch는
`frame_start_ns + hidden_top_rows * line_time_ns`인 첫 full-sensor 활성 행의 시작이다.
관측 준비는 마지막 full-sensor 행의 노출 종료를 기준으로 계산한다.

```text
full_sensor_exposure_end_ns
  + readout_tail_ns       → readout_complete_ns
  + frame_transport_ns    → frame_available_ns
  + statistics_ns         → statistics_ready_ns
  + controller_ns         → controller_complete_ns = issued_at_ns
  + command_transport_ns  → command_arrival_ns
  + actuator_ns           → actuator_ready_ns
```

명령은 최소 프레임 지연을 지나고 `actuator_ready_ns <= latch_at_ns`인 프레임에만
적용한다. 정확히 같은 시각은 허용하지만 최소 지연이 1 이상이므로 현재 관측 프레임에는
적용되지 않는다. 여러 명령이 가능하면 가장 최근 source frame을 선택하고 나머지를
`superseded`로 기록한다. 이미 적용된 것보다 오래된 source의 뒤늦은 명령은 `stale`이다.

최대 프레임 지연을 넘긴 도착 명령은 `expired`이며, 명령이 도착하기 전에 과거 시각으로
만료 처리하지 않는다. 마지막 latch 이후에도 적용되지 않은 명령은
`pending_after_sequence`로 보존한다. `unapplied_reasons`에 최소 프레임 지연,
미도착, actuator 미준비, 최대 지연 경과 등을 구분한다. 이것은 실행 Job의
`queued/running/succeeded/failed/cancelled` 상태와 별개의 명령 상태다.

## 하나의 PD·난수 세션

같은 full-native 센서의 PD 상태와 고정 PRNU/dark pattern을 전체 작업 동안 유지한다.
초기 PD는 각 행의 첫 노출 시작 시점에 0이다. 이 상태를 공통 wall-clock 한 시각의 센서
snapshot으로 해석하지 않는다. 출력 상태 시간 기준은
`per_row_post_transfer_not_common_wall_time`이다.

광전하는 실제 노출 window에만 유입되고 dark generation은 프레임 간 gap에도 이어진다.
노출 종료 때 부분 transfer, 선택한 프레임의 노출 시작 때 PD clear를 수행한다. 같은 시각의
순서는 `transfer → clear → open`이며 read-node reset은 매 read마다 별도다.
`pd_clear_before_exposure`는 frame ID에 연결된 **미리 고정한 목록**이다. 프레임별 값은
다를 수 있지만 AE가 이 목록을 변경하지 않는다. PD release/decay/trap 모델은 없다.

Runtime은 각 latch에서 결정한 노출로 장면과 PWM을 다시 적분하고, 실제 행별 gap으로
잔류 전하를 전진시킨 뒤 전기 readout을 한 번 수행한다. 명령 숫자만 바꾸거나 이미
만든 RAW에 노출 배율을 곱하는 방식이 아니다. Constant/PWM의 clock·위상은 장면 계산의
입력이고 제어기의 관측 입력은 아니다.

`numpy.SeedSequence(seed).spawn(2)`로 charge와 readout 난수 stream을 나눈다.
각 stream은 전체 sequence 동안 유지되고 매 프레임 재생성하지 않는다. 재실행은
동결된 입력과 새로운 초기 0 상태에서 시작하며 외부 reservoir를 이어받지 않는다.
`noise_enabled=false`는 연속 전하의 결정론적 흐름이다. 포화가 있는 확률 모델의
기댓값이라는 뜻은 아니다. 서로 다른 적용 노출의 두 실행은 같은 seed라도 난수 호출과
전하 분포가 달라질 수 있으므로 모든 표본이 일대일 대응한다고 가정하지 않는다.

## 저장 프로그램·Test·Worker·화면

1. 같은 Camera Assembly와 native 시간 장면에 대해 frame starts/mode와 균일한 초기
   exposure/gain/ISP 설정을 등록한다.
2. `GET /api/v1/templates/capture-program?assembly_id=…&mode=electrical_control&control_sequence_id=…`
   로 초안을 얻는다. 기본 grid는 초기 노출 근처에서 만든 작성 예이며, scene coverage와
   frame 간격을 검토해 조정해야 한다. 템플릿 생성만으로 실행 가능성이 판정되지는 않는다.
3. `POST /api/v1/capture_programs`로 정책·전기 모델·PD·파형·출처를 새 버전으로 고정한다.
   원문과 정규화 프로그램 JSON을 각각 artifact로 보존한다.
4. 같은 `assembly_id`, `scene_sequence_id`, `control_sequence_id`, `capture_program_id`로
   `GET /api/v1/templates/temporal-test`를 얻어 Test를 등록한다.
5. `POST /api/v1/runs`에 `plugin_id: electrical_control_v1`과 위 ID·Test를 제출한다.
   가변 parameters는 `seed`, boolean `noise_enabled`뿐이다. SDK에서는
   `submit_electrical_control(assembly_id, test_id, scene_sequence_id, control_sequence_id,
   capture_program_id, seed=…, noise_enabled=…, force=False)`를 사용한다.
6. `GET /api/v1/runs/{id}`, `/export`, `/bundle`로 원본과 증거를 읽고,
   `POST /api/v1/runs/{id}/cancel`로 취소한다. Worker timeout/취소 후 부분 PD 상태를
   새 작업에서 재개하는 기능은 제공하지 않는다.

준비 단계는 고정 모델들의 전체 AE 노출 구간 유효성, source/normalized 프로그램,
Test·sequence·clock 일치, full-native geometry, same-row overlap, 최소/최대 노출의
장면 coverage, 정수 시각과 dark 생성 한도, 배열·trace 예산을 검사한다. 최대 광학 생성
한도는 Worker가 실제 광학 rate를 구한 뒤 난수·PD 상태를 전진시키기 전에 확인한다.
Worker는 등록 원본에서 요청을 다시 구성하고 동결 요청·실행 환경과 대조한다.
캐시 재사용도 새 프로그램이나 변경된 원본을 대신하는 경로가 아니다.

현재 최대 16프레임과 full-native 짝수 격자를 사용한다. 개별 배열 128MiB,
전체 증거 배열 192MiB, 작업 배열 추정 1GiB와 제어 trace 1MiB의 상한이 있다.
이 수는 실제 peak memory 측정값이 아니며 상한 초과 시 자동 축소하지 않는다.

Workbench의 Factory에는 **전달 RAW 관측 · 노출 AE 초안**이 있고 target DN과 노출 grid를
편집할 수 있다. Enabled·ROI·사이트·지연·전기 모델·PD clear는 전체 JSON에 명시한다.
실행 화면은 입력 sequence를 “프레임 시각과 초기 설정”으로 설명하고, 결과 화면은
실제 적용 노출, 해당 관측의 제안 노출, signed DN, 정책 판정, 적용 명령과 latch를 구분한다.
명령 표와 전체 JSON에는 source frame, ready/applied 시간과 최종 미적용 이유가 남는다.
이 UI 계약의 구현과 실제 브라우저 검증·설치 인도는 별도의 근거로 관리한다.

## 결과·비활성 기준선·판정 한계

`raw_groups.npy`는 `[frame, 1, height, width]`의 원본 integer RAW다.
`applied_exposure_ns.npy`, 행별 start/end/gap, 적분 가중치, 전기 readout 단계와
`retained_*` 전하 ledger를 함께 저장한다. 각 전하 전이의 전후 상태 해시와 보존 계산은
모델 진단 근거이며 제어기 입력이 아니다. `summary.electrical_control.frames`와
`commands`가 명령·실제 적용·관측을 연결한다.

Preview는 고정 ISP의 black-to-white 코드 좌표에서 만든 전달 신호 표시다. 비선형 RAW도
원래 `response_type`을 결과에 보존하며, 코드 표시용 adapter가 현재 광전하를 복구했다고
주장하지 않는다. `hdr_merge_applied=false`, `current_exposure_charge_estimation=false`다.

고정 제어와 비교하려면 동일한 scene/clock/scaffold/model/PD clear/seed에서 정책
`enabled`만 `false`로 만든 **별도 고정 프로그램·Test**를 실행한다. 비활성 실행도 같은
PD/readout 경로를 거치며 초기 노출을 유지한다. 먼저 noise를 끈 조건에서 실제 적용값과
RAW/RGB 차이를 확인하고, 잡음이 켜진 조건은 분포와 seed 정책을 고려해 해석한다.
현재 이 비교를 자동으로 묶는 전용 최적화·paired-run 기능은 없다.

`target_outcome`은 관측 프레임 수, 구간 안 프레임 수, 마지막 연속 구간 안 프레임 수,
그 구간의 첫/마지막 `statistics_ready_ns` 차이와 상·하한을 벗어난 최대 DN을 기록한다.
첫/마지막 관측 간 span은 구간 사이의 연속 신호 유지 보증이 아니다. 선언한 step/window가
없으므로 settling은 `undefined_no_declared_step_window`, 실제 안정성과 LFM 성능은
`not_established`다. PWM의 교대 응답을 정상광 수렴이나 LFM 개선으로 바꾸어 표현하지 않는다.

일반 정적 RAW 실측 비교기는 이 결과에 `closed_loop_comparison_operator_required`를
남기고 물리 검증 PASS를 내지 않는다. 현재 노출의 광전하를 추정하는 기존 ISP 경로도
명시적으로 거부한다. 별도 retained-code ISP 재처리/비교의 source 계약은 아직
`retained_charge_v1` 원본만 허용하므로 전기 AE 결과가 자동으로 연결되지는 않는다.

DCG/HCG·LCG 선택 또는 융합, gain 제어, AWB, HDR 합성, 광원 위상 추정,
LFM 최적화, 일반 동적 ISP 제어와 장비 레지스터/전송 검증은 이번 범위 밖이다.
독립 실측과 불확실도 정량화 없이는 실제 AE 안정성·물리 정확도·상용 LFM 성능을
인증하지 않는다. 이 기능의 계산 성공은 일곱 모듈·네 실행 방식의 전체 상용화 완료를
뜻하지 않는다.

구현과 검증 진입점은 [순수 정책](../src/camera_platform/electrical_control.py),
[캡처 Runtime](../src/camera_platform/electrical_control_runtime.py),
[계획/실현 계약](../src/camera_platform/electrical_control_workflow.py),
[정책 시험](../tests/platform/test_electrical_control.py),
[Runtime 시험](../tests/platform/test_electrical_control_runtime.py),
[독립 수치 시험](../tests/platform/test_electrical_control_independent.py),
[서비스 연결 시험](../tests/platform/test_electrical_control_workflow.py),
[UI 시험](../tests/platform/browser/electrical-control.cjs)이다.
최종 시험 계수와 실행 환경·소스 해시는 별도 확정 증거를 따른다.

## 2026-09-24 소스 검증

관련 Python 230개와 화면 로직 33개를 통과했다. 실제 API/Worker에서 고정 노출,
선형 AE, 비선형 AE를 실행했고, 브라우저에서 새 정책을 편집·등록하고 Test를 고정해
네 번째 실행을 완료했다. 200–250 DN 목표의 작성 사례에서 실제 노출은
4→3.2→2.4→2.4 ms, 관측은 282.086→282.086→225.680→214.391 DN이었다.
노출 감소 직후 잔류 전하가 관측에 남는 결과다.

브라우저 실행의 원본 산출물 52개와 번들을 HTTP로 검사했다. API/Worker 재시작 후
기록과 원본 산출물, ZIP 내부 64개 파일이 동일했으며 실제 화면에서도 같은
frame-0002의 2.4 ms·16×16 PNG를 확인했다. 압축 포장 시각 때문에 ZIP 파일 자체의
바이트 동일성은 보장하지 않는다. QA 프로세스와 작업 탭을 종료하고 기본 앱 보존을 확인했다.

전체 제품 회귀 또는 설치 완료가 아닌 **작성된 16×16·4프레임 소스 기능 검증**이다.
[검증 요약](../reports/camera_platform/electrical-control-v0161/evidence-summary.json),
[현재 진행 상태](../reports/camera_platform/progress-20260924.md)를 참고한다.

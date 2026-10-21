# HDR/LFM 모드·기준자료 준비도

`GET /api/v1/hdr-lfm-mode-readiness/{identity}`는 완료된 R1 시간 RAW,
Lens×Sensor 정적 HDR, PWM/LFM RAW 또는 그 RAW의 HDR 재구성 Job을 다시 읽는다.
각 Job의 원본 artifact와 동결된 센서 revision을 검증한 뒤 모델 내부 계산,
센서 운영점의 출처, 실제 모드 실행, 독립 기준자료를 **별개 상태**로 반환한다.
SDK에서는 `client.hdr_lfm_mode_readiness(job_id)`를 사용한다.

현재 선택된 센서 DB의 [모드 자산 집계](../reports/camera_platform/integrated-workflows-20260928/hdr-lfm-sensor-mode-audit.json)는
최신 224개 보고서 중 simulated 144개, 그중 일반 readout 가정 141개와
카메라 수준 proxy 3개를 기록한다. 실행 가능한 144개 어느 것도
`readout_profile.provenance.conditions`에 노출 조건이나 readout FPS를
제시하지 않는다. 세 proxy의 LCG 표시는 모드별 HDR/LFM 시간표가 아니다.
따라서 저장된 렌즈·센서만 고른 뒤 제품 HDR/LFM 결과를 승인할 수 없다.

R1 합성 시간 RAW의 `model_execution`은 실제 원본 배열 재조회·해시 검사 결과지만,
`sensor_operating_point=null`이다. 합성 정적 HDR 성공은 실제 센서 모드 성능으로
승격되지 않는다. Lens×Sensor 정적 HDR는 원본 두 RAW와 fusion 입력을 검증하지만
타임스탬프가 없는 독립 reset 브래킷이다. PWM/LFM의 기존 출처 결속 timing mode는
문서 원본의 SHA와 센서 revision·readout·노출 쌍을 연결해 다음 Job에서 사용할 수
있으나, 그 문서 속 line-time 수치를 독립적으로 판독·검증하지는 않는다.

실행 가능한 다음 경로는 준비도 결과의 `next_workflows`에 포함한다. PWM/LFM RAW
Job에서 출처 결속 모드가 없으면 `timing-modes/template`으로 해당 센서·가상 창·
노출 쌍의 binding을 만들고, 원본 문서와 mode JSON을 `timing-modes/import`에 넣는다.
반환된 SHA를 `central_bounded_timed` 명세의 `timing_mode_sha256`에 적용해 새
Lens×Sensor LFM Job을 제출한다. 그 원본 RAW는 `lens-sensor-pwm-hdr` Job에서
재구성할 수 있다. 이 체인도 제품 모드 실행은 아니다.

출처 결속 timing을 쓰는 완료된 PWM-HDR Job에는
`GET /api/v1/hdr-lfm-reference-comparisons/template/{identity}`가 **정확한
원본 Job·보고서 해시·센서 운영점·노출·PWM 조건·배열 형상**을 가진 intake를
반환한다. `client.hdr_lfm_reference_template(job_id)`로 같은 값을 얻는다. 제출에는
다음 다섯 개의 실제 파일과 완전한 metadata가 필요하다.

1. 원본 undemosaiced `int32` NPY: `[2 exposure,4 phase,H,W]`, 영상·RAW 변환 없음.
2. 별도 획득·교정한 `float64` 기준 광전자율 NPY: `[4 phase,H,W]`, 단위는
   `electron/pixel/s at PWM on luminance`.
3. 물리 센서 serial·장비 ID/교정 revision·native crop origin과 8개 RAW 프레임의
   순서·노출·위상·파일 내 프레임 해시를 선언한 UTF-8 JSON mode log.
4. mode log 해시, line time, PWM 주기·duty·level·phase를 선언한 UTF-8 JSON
   waveform log.
5. 두 log 해시, 기준 rate의 네 위상 순서·파일/위상 해시, 별도 계측기와 교정
   불확실도를 선언한 UTF-8 JSON reference log.

세 log의 정확한 schema 이름·필드 목록은 intake의 `packet_contract`에 나온다.
`mode_log`는 `camerae2e_hdr_lfm_acquisition_mode_log_v1`, `waveform_log`는
`camerae2e_hdr_lfm_waveform_log_v1`, `reference_log`는
`camerae2e_hdr_lfm_reference_rate_log_v1`이다. 각 log는 `source_uri`와 원본 HDR
Job ID/보고서 SHA를 담고, 다른 두 log가 앞 log의 SHA를 참조한다.

`mode_log.frame_order`는 NPY의 C-order `[2 exposure,4 phase,H,W]` 순서대로
8개 항목을 둔다. 각 항목은 `array_index=exposure_index*4+phase_index`, 고유
`frame_id`, `acquisition_order` 0–7, 증가하는 `row_zero_timestamp_ns`, Job에
동결된 `exposure_s`·`phase_fraction_of_period`·`row_zero_phase_s`, 해당
`int32` 프레임의 C-order byte SHA를 담는다. `reference_log.phase_order`의 4개
항목은 같은 위상 index/분율과 각 `float64` rate 평면의 byte SHA를 담는다.
선택한 모델은 가상 창의 2×2 RGGB CFA를 원점 [0,0]에 고정하므로, 새 packet의
native crop은 짝수 행·짝수 열에서만 받아들인다. 이는 **CFA 위상 일치 조건**이며
가상 창이 실제 die의 그 위치를 촬영했다는 공간 정합 증거는 아니다.

`reference_log.calibration`에는 RAW 장비와 다른 기준 계측기 ID, 교정 revision,
방법·버전, 교정 문서 SHA, 0보다 크고 1 이하인 선언 표준 상대 불확실도,
기준 rate 배열에 교정을 적용했다는 표시를 넣는다. 업로드 RAW나 모델 출력을
기준 rate 생성에 사용했다는 표시는 거부한다. 교정 문서의 **바이트 자체는 이
intake가 받지 않으므로**, 문서 SHA·수치·독립성은 검증된 사실이 아니라
제출자의 선언이다.

세 log의 SHA-256·출처 URI는 metadata의 `sources`에 입력한다. 원본 mode 문서의
SHA와 센서 운영점, 동결된 RAW Job의 노출·파형 조건, 업로드 배열의 전체/위상별
해시는 제출 전에 교차 검사한다. 파일이나 조건이 없거나 모순되면 Job을 생성하지
않는다. 측정 RAW가 시뮬레이션 RAW의 완전한 복사본인 경우도 거부한다. 일관된
packet도 출처 진위, 기록된 timestamp·line time의 실측 여부, 기준자료의 독립성을
증명하지 않는다. 기존 저장된 v1 비교 Job은 당시의 불투명 log 해시 계약으로
계속 재조회할 수 있으며 새 제출만 구조화된 log를 요구한다. 제출은
`POST /api/v1/hdr-lfm-reference-comparisons`의 multipart `metadata`,
`measured_raw`, `measured_reference_rate`, `mode_log`, `waveform_log`,
`reference_log`이다. SDK의 `submit_hdr_lfm_reference_comparison`은 이 파일 경로를
받는다. `GET /api/v1/jobs/{id}` / `client.wait(id, expected_kind=...)`와
`GET /api/v1/hdr-lfm-reference-comparisons/{id}`로 Worker 결과를 재조회한다.

Worker는 모든 입력과 원본 HDR Job을 다시 검증하고 모델·입력 RAW의 **프레임 전체
평균 DN**, 재구성 광전자율의 **모델 유효 픽셀 평균**과 독립 기준 광전자율의
**위상 전체 평균**을 별도 기록한다. 두 rate 평균의 표본 지지가 다르므로 직접적인
오차율로 해석하지 않는다. 현재 가상 창의
native die 위치와 공간 정합은 입증되지 않았으므로 픽셀별 오차를 계산하지 않는다.
새 결과의 `packet_consistency.internal_consistency`는
`verified_against_uploaded_arrays_and_frozen_job`이고,
`packet_consistency.source_authenticity`는
`declared_not_independently_verified`이다. 해시가 출처의 진위나 기준자료의
독립성을 증명하지도 않는다. 결과는
`comparison_scope=unregistered_global_means_only`,
`physical_validation=not_established`, `product_hdr_lfm_decision=inconclusive`로
고정된다. 실제 측정이 제출돼도 독립 출처 검토, 타이밍 검증, 광학·픽셀 결합,
불확실도와 대표 조건의 반복이 뒤따라야 제품 판정이 가능하다.

새 제출의 template에는 `registration_qualification_contract`도 포함된다.
Worker 결과의 `registration_qualification`은 업로드된 2×4 frame label·CFA
위상·파일 해시를 검증한 packet과 동결된 HDR/LFM 보고서를 결합해, 각 프레임의
**제출자가 선언한 시간축**에서 첫 행 시작과 마지막 행 노출 종료 시각을 계산한다.
`declared_timing_window.first_start_ns`부터 `last_end_ns`까지를 실제 PWM
trace가 덮어야 한다는 작업 요구 범위로 사용할 수 있다. 이 값은 계측기 시각이나
위상 동기화가 검증됐다는 뜻이 아니다. 원본 모델의
`configured_sensor_scope=virtual_simulation_window_only`와 native crop mapping
부재도 함께 기록하므로, 단순한 배열 형상 일치가 공간 정합으로 바뀌지 않는다.

`status=blocked_pending_independent_registration_evidence`의
`requirements.required_inputs`는 다음 여섯 자료를 명시한다.

1. 가상 모델 픽셀 중심에서 실제 센서 좌표까지의 출처 결속 변환·ROI·CFA 위상·오차.
2. 네 위상 각각의 기준 영상에서 RAW까지의 정합 변환, 겹침 mask, 잔차·불확실도와
   이를 재현할 원본 영상 또는 fiducial.
3. RAW timestamp clock에 동기화돼 모든 행의 노출 구간을 덮는 PWM 파형 또는
   trigger-edge trace, clock offset·오차.
4. 같은 clock의 기준 영상 획득 시각·적분 구간과 네 rate 평면의 연결.
5. 해당 센서 모드의 실측 black level, DN→전자 gain, 포화·잡음·FPN 교정과 오차.
6. 원본 장비·교정 기록, 동일 장면·렌즈 구성, 출처의 독립 검토.

현재 multipart 계약은 이 자료를 받지 않으므로 결과는 항상
`pixelwise_error_available=false`, `measured_fusion_error_available=false`이다.
필요 자료를 선언 문자열로만 채운다고 gate가 통과하지 않는다. 출처와 정합을
검사하는 후속 계약이 마련되기 전까지 모델 fusion과 실제 RAW fusion의 픽셀별
차이나 제품 HDR/LFM 성능을 보고하지 않는다. 기존 완료 Job에는 새 필드를
소급 생성하지 않으며 당시 비교 범위로 그대로 재조회한다.

개발 검증은 `tests/platform/test_lens_sensor_lfm_job.py`의 출처 결속 timing →
PWM RAW → HDR → 준비도 → 별도 Worker 비교 체인을 실행한다. 테스트의
`fixture://` 파일은 API·해시·수치 연결만 검증하며 물리 측정이 아니다.

저장된 한 번의 실행 결과는 [bounded readiness proof](../reports/camera_platform/integrated-workflows-20260928/hdr-lfm-mode-readiness-bounded.json)에 있다.
이 보고서는 실제 Registry에 보존된 PWM RAW, HDR, 기준자료 비교 Job의 ID·입력
SHA-256·준비도 차단 코드와 `state_dir`를 기록한다. 선택 센서의 원본 DB 점검은
[mode audit](../reports/camera_platform/integrated-workflows-20260928/hdr-lfm-sensor-mode-audit.json)에
따로 있다. 보존된 fixture 비교 Job의 `status=succeeded`는 제출·Worker·재조회
연결이 동작했다는 뜻이며, 보고서의 `physical_validation=not_established`와
`product_hdr_lfm_decision=inconclusive`가 물리적 결론이다. 사용한 timing 문서,
RAW, 파형 log, 기준 rate는 모두 테스트 fixture이며 제품 자료가 아니다.

[dev32 wheel을 격리 설치한 동일 체인의 실행](../reports/camera_platform/integrated-workflows-20260928/dev32-installed-hdr-lfm/hdr-lfm-mode-readiness-bounded.json)도
통과했다. 이 설치본 실행 역시 `fixture://` 출처의 선언적 timing과 합성
기준자료를 사용했고, 비교 범위는 `unregistered_global_means_only`, 제품
판정은 `inconclusive`다. 이 보존 dev32 결과는 구조화된 packet 도입 **이전**의
불투명 log 계약으로 생성됐다. 새 JSON log 계약의 검사는 아래 테스트를 새로
실행한 결과로 판단해야 하며 과거 결과를 소급 승격하지 않는다.

같은 경로의 새 보존 실행을 만들려면 저장소 루트에서 다음을 실행한다. 테스트는
`state-<uuid>` 디렉터리를 생성하고 최신 실행의 링크를 proof JSON에 갱신한다.

```sh
CAMERA_PLATFORM_HDR_LFM_PROOF_DIR=reports/camera_platform/integrated-workflows-20260928 \
  .venv-platform/bin/python -B -m pytest -q \
  tests/platform/test_lens_sensor_lfm_job.py::test_imported_timing_mode_drives_saved_lfm_and_hdr_worker_chain
```

센서 DB 집계는 다음 명령으로 다시 생성한다. 스크립트는 현재 선택 revision의
저장 결과 해시를 하나씩 검사하며, 144개 simulated run에 저장되지 않은
exposure/FPS 값은 추정해 채우지 않는다.

```sh
PYTHONPATH=src .venv-platform/bin/python -B \
  tools/audit_camera_hdr_lfm_sensor_modes.py --root camerae2e_db \
  --output reports/camera_platform/integrated-workflows-20260928/hdr-lfm-sensor-mode-audit.json
```

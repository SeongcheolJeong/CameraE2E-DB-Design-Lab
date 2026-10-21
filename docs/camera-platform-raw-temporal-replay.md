# 선택형 RAW 시간 필터 재처리 — 2026-09-26 작업 트리

## 이번에 가능한 작업

기존에 완료된 retained/electrical 촬영의 single/HCG/LCG 중 한 경로를 선택하고,
실제 취득 시각·노출·gain을 유지하면서 `raw_temporal` 필터를 별도 Worker에서 실행한다.
원본 정수 RAW, 처리된 float64 DN, 적용 마스크, 추가 표본 나이, 처리 차분을 별도 NPY로 저장한다.
`/raw-temporal.html`에서 원본/처리/마스크를 같은 프레임으로 비교하고 저장 결과를 다시 연다.
기존 ISP 모델, 프로필 v1의 의미, 기본 설치 앱과 자동 인지 입력은 변경하지 않는다.

**이 모듈은 기본 비활성이다. 실제 두 프레임 주기의 장면 변화를 지울 수 있다.**
POST 요청에도 `acknowledge_period_two_risk: true`를 명시해야 한다. 목록 선택이나 페이지
재열기로 자동 처리하지 않는다. 0.98/1.02 실제 교대 장면을 평탄화한 이전 실패 사례를
화면에서 계속 보여준다. 이것은 처리 사용을 승인하는 경고 해제 절차가 아니라 연구용 동의다.

## 취득과 처리의 경계

`retained_isp_source`의 기존 검증 경로를 재사용한다. 전기 AE는 원본 RAW 기반 제어
재실행으로 초기 계획이 아닌 실제 적용값을 확인한다. 전체 전하/트랩 상태는 원본
기록의 근거일 뿐 필터 입력으로 사용하지 않는다.

시간 기준은 실제 `row_transfer_ns`다. 첫 행 전달 시각을 기준으로 상대 시각을 만들되
원래 signed 정수 시각과 모든 행의 전달 시각을 결과에 보존한다. 설정 비교에는 노출,
gain, 선택 readout, 입력 형식, 센서 좌표, 행 오프셋과 관련 취득 조건을 포함한다.
프레임 ID·명령 ID는 설정값으로 오인하지 않는다.

노출/gain/관련 설정 변화, 기대 간격과 다른 행별 전달 간격, PD clear에서는 이력을
초기화한다. 연속된 PD clear도 매번 초기화한다. 초기화 뒤 최초 세 프레임은 통과한다.
현재 프레임과 과거 세 RAW만 사용하며 미래 표본을 기다리지 않는다. 평균된 픽셀에는
실제 이전 행 전달 간격의 절반을 `added_sample_age_ns`로 표시한다. 평균하지 않으면 0이다.
이 값은 필터가 추가하는 명목 정보 나이이지 하드웨어 실행 지연 측정이 아니다.

미리보기는 동일 black/white 범위의 그레이스케일이며 최대 512px다. 사용자 표시 배율은
두 영상에 공통 적용되고 NPY와 수치 평가를 바꾸지 않는다. 처리값은 0.5 DN을 보존하며
정수 센서 RAW로 다시 표시하거나 모델의 물리적 역보정 결과라고 부르지 않는다.

목록 조회는 저장된 요약만 반환한다. 선택한 상세·실행·내보내기에서 원본/결과를
검증하며 목록을 보기 위해 과거 모든 RAW를 다시 측광하는 구조는 사용하지 않는다.

## 실행

별도 개발 상태는 `outputs/camera-platform-raw-temporal-dev-20260926`이다.
원래 합성 QA registry를 SQLite backup으로 복제하고 blobs를 별도 복사했다.
복제본에 원래 조건의 연속 16프레임 취득과 처리 결과가 저장돼 있다.
초기 준비의 장면 참조 오류는 수정했고, 최초 실패 복제본/로그는 별도로 보존했다.
기본 설치본 `outputs/camera-platform`과 8770 포트는 수정하지 않았다.

프로젝트 루트에서 다음 명령으로 개발 작업실을 시작한다. 기존 개발 상태가 있으면
예제 취득을 다시 실행하지 않는다. 프로세스는 foreground이고 Ctrl-C로 API/Worker를 종료한다.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-platform/bin/python \
  tools/run_camera_raw_temporal_dev.py --serve
```

브라우저 주소는 `http://127.0.0.1:8796/raw-temporal.html`이다.
저장한 비교는 이름 `RAW replay · 실제 Worker와 화면 연결`,
ID `run-e8452334e2394352a9ebcff2ceb1621c`로 선택할 수 있다.

API: `GET /api/v1/raw-temporal/sources`, `POST/GET /api/v1/raw-temporal/replays`,
`GET /api/v1/raw-temporal/replays/{id}` 및 `/{id}/export`.
SDK: `raw_temporal_sources()`, `submit_raw_temporal(...)`, `raw_temporal_replay(id)`.
기존 보안 토큰·동일 origin 제한은 그대로 적용한다.

## 이번 결과와 확인 범위

별도 실제 Worker에서 16프레임 취득 1건과 RAW 재처리 1건을 완료했다.
취득의 첫 4프레임은 원래 보존 RAW와 dtype/값이 정확히 같다.
처리 전 후반 green 측광값 584.625/608.90625 DN의 교대가 처리 후 596.765625 DN으로
일정해졌다. 상대 변화폭 4.068808% → 0%, 평균은 보존됐다. 앞 세 프레임은 원본과
정확히 같고 후반 마스크는 100%, 최대 추가 정보 나이는 10 ms였다.
새 물리 실험 캠페인이 아니라 앞서 만든 알고리즘을 등록 취득/Worker/화면으로 연결한 결과다.

관련 최종 시험 33개, Chromium 실제 화면 확인 15항목이 통과했다. 원본 선택/명시적
제출/실제 Worker 결과/세 미리보기/390px 모바일 폭/새 작업 없는 재열기를 확인했다.
경로 우선순위 충돌과 HTML 기본 step 제약 오류는 실제 화면 확인에서 발견해 수정했다.
화면 재열기 대기 중 DOM 접근 오류는 검증 도구에서 수정했다. 초기 실패 로그도 보존했다.
검증용 API/Worker/브라우저는 종료했고 개발 상태와 결과만 남겼다.

[실제 연결 결과](../reports/camera_platform/raw-temporal-replay-20260926/integration-results.json)
· [최종 시험](../reports/camera_platform/raw-temporal-replay-20260926/final-tests.xml)
· [브라우저 확인](../reports/camera_platform/raw-temporal-replay-20260926/browser-passed/browser-qa.json)

일반 장면 보존, 실물 LFM, 목표 DN 자동 제어, 실시간 실행, 대형 해상도, 고객 배포는
완료된 것으로 표시하지 않는다. 다음은 이 작은 PWM 예제를 반복 확장하기보다 우선순위
문서의 일반 광학/픽셀/전기 응답을 두 조건에서 RAW까지 대조하는 작업으로 돌아간다.

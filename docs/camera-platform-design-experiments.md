# 새 설계 실험 — 명시적 입력, 별도 데이터, 후보 선택과 평가

## 현재 개발 소스: `0.16.2.dev3`

R1 설계 실험의 Agent 경로는
`client.design_experiment_template()` → specification에서 모델·장면 ID 선택 →
`client.submit_design_experiment(spec)` → `client.wait(job["id"])` →
원본 artifact 조회다. 공통 `GET /api/v1/jobs/{id}`가 작업 상태를 제공하고,
완료된 설계 실험은 `GET /api/v1/design-experiments/{id}`의 전문 검증 결과를
반환한다. 이 개발 소스에는 [기존 DB revision 비교](camera-platform-db-integration.md)도
별도 흐름으로 추가됐다. 렌즈 기하 결과와 센서 RAW는 물리 결합되지 않는다.
아래의 RC2 설치 후보 설명은 고정된 배포물을 뜻하며 이 개발 소스와 같은
지원 범위를 주장하지 않는다.

## 현재 설치 후보: 0.16.1rc2 (2026-09-27)

새로 시작할 때는 [RC2 시작 안내](camera-platform-r1-rc2-quickstart.md)와
[지원 범위](camera-platform-r1-rc2-support.md)를 따른다. 별도 R1 프로젝트를
`init-r1`로 만들고, 패키지에 고정된 모델·장면 ID를 선택해 같은 작업실에서
후보 계산, 정수 RAW/기본 ISP RGB, 선택·평가, 저장·재열기를 수행한다.
원본 RAW의 WB·CCM·tone 재처리와 한 등록 예제의 시간/HDR/DCG 파일럿도
RC2에 포함된다. 설치·Worker·브라우저·복구는
[최종 설치 증거](../reports/camera_platform/r1-rc2-20260927/installed-proof-release/install-evidence.json)에
기록했다.

현재 R1 모델 6개는 서로 다른 렌즈·센서 제품 6개가 아니다. 기존 Lens/Sensor DB
revision을 자유롭게 선택하거나 MTF·색·인지 지표로 전역 설계 결정을 내리는
경로는 아직 지원하지 않는다. 이 문서의 다음 절은 **이전 개발 상태의 구현·시험
이력**이며 아래의 `tools/run_camera_design_experiments.py` 명령과 개발 state를
RC2 설치 시작 절차로 사용하지 않는다. 기본 v0.15 설치 상태도 RC2와 별개다.

## 이전 개발 기록

**2026-09-27 현재 상태: 관련 시험과 실제 Worker/API/브라우저 연결 확인 완료, 미배포.**
일부 셸 요청은 여전히 보안 상태 판정에서 차단됐지만 명시적인 pytest/실행 명령은
이번에 성공했다. 38개 관련 시험과 실제 Chromium 17항목이 통과했다. 전용 개발
state에서 사용자가 입력하는 작은 실험 한 건(선택용 4회+평가용 1회)을 실행했다.
[현재 실행 근거](../reports/camera_platform/design-experiments-20260927-execution/evidence-summary.json)를 따른다.
이전의 구현 저장·실행 차단 기록은 당시 상태로 보존하며 기본 앱 승격으로 해석하지 않는다.

## 이번 구현

`design_experiments.py`는 기존 `ArchivedPrescription`, `evaluate_response`,
`select_candidate`를 재사용한다. 새 solver나 후보 계획 전용 계층을 만들지 않는다.
명시적인 POST 한 번이 기존 registry/Worker의 `design_experiment` 작업 한 건을 만든다.

1. 연구용 이름, 투과 조건, 노출/gain 후보, 선택용 광입력, 다섯 제약을 고정한다.
2. 노출 상한 밖 후보는 제외하고 나머지의 PSF/전하/정수 RAW를 실제 계산하는 경로다.
3. 선택용 결과만 기존 선택기에 전달한다. 적합 후보가 없으면 선택 없음으로 남긴다.
4. 선택 JSON을 artifact로 저장한 뒤 선택에 쓰지 않은 변형 입력과 잡음 반복을 계산한다.
5. 평가가 실패해도 재선택하거나 문턱값을 바꾸지 않는다.

각 취득의 원본 정수 RAW NPY와 진단 NPZ, 사전 계획, 평가 전 선택, 결과 JSON,
CSV를 별도 개발 registry의 blob에 저장한다. 이번 실제 Worker 작업에서도 생성·재조회했다.
동일한 이름/입력/환경의 중복 POST는 registry의 기존 작업을 재사용한다.
새 이름 또는 변경한 입력은 새 기록이다. 과거 연구 결과를 수정하지 않는다.

## 실행 범위

기존 합성 구면 처방 clear / quarter / radial 자산만 사용한다. 동일 광휘·센서·
native crop의 광입력에 이미 반영된 E/L을 다시 곱하지 않는다.
선택 목표는 최소 Green 코드, 예측 SNR p10, well/최종 코드 포화, 노출 상한이다.
예측 SNR은 모델식이며 실측값이 아니다. 움직임·색 정확도·perception은 이 선택의
목적에 들어가지 않는다. 앞서 움직임 기준으로 선택이 달라진 결정은 그대로 남긴다.

평가용 입력은 명시된 배율과 상하 반전 또는 좌우 반전/파장 비율 변환이다.
이번 선택에 사용하지 않았다는 의미이지, 과거에 본 적 없는 blind 데이터는 아니다.
이를 보고 설정을 바꿨다면 해당 평가는 개발 데이터로 취급하고 새 독립 평가를 준비해야 한다.

원래 등록 모델의 지원 범위를 확대하지 않는다. 직접 Runtime 연구 가정을 사용하며,
독립 실물 측정·실기 gain 지원·온도·움직임·HDR·trap 검증을 추가로 주장하지 않는다.

## 별도 개발 데이터와 실행 명령

기본 state는 `outputs/camera-platform-design-experiments-default`다.
POST와 조회는 이 이름 계열의 전용 개발 state에서만 허용한다. 기본 앱의
`outputs/camera-platform`에서는 이 실험을 만들지 못하도록 했다.
등록 모델 복제, 기존 DB migration, 설치 승격은 하지 않는다.

관련 시험 재실행 명령(이번 실행 결과는 별도 근거 폴더에 보존):

```sh
cd /Users/seongcheoljeong/Documents/CameraE2E
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-platform/bin/python -m pytest -q \
  tests/platform/test_design_experiments.py \
  tests/platform/test_constrained_selection.py \
  tests/platform/test_response_chain.py
```

작업실 실행:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-platform/bin/python \
  tools/run_camera_design_experiments.py --serve
```

같은 Mac에서 `http://127.0.0.1:8798/design-experiments.html`에 접속한다.
`--serve` 없이 실행하면 설정만 출력하고 DB나 Worker를 만들지 않는다.
첫 실제 확인은 `--state-label smoke`의 별도 state에서 투명 조건, 노출 1/2 ms,
gain 1, 선택 배율 1/2, 평가 배율 1.5, 잡음 반복 0으로 작은 작업을 수행한다.
새 전체 격자 연구를 먼저 반복할 필요는 없다.

## API와 SDK

- GET `/api/v1/design-experiments/template`, GET `/api/v1/design-experiments`
- POST `/api/v1/design-experiments`: 명시적 local session token/연구 확인 필요
- GET `/api/v1/design-experiments/{id}`, GET `.../{id}/export`
- GET `.../{id}/cases/{case_id}`: 선택한 RAW만 검증/미리보기
- POST `.../{id}/cancel`: 기존 registry 취소 경로

목록은 메타데이터만 읽는다. 상세는 저장 결과·고정 입력을 비교하고, 사례를 열 때
원본 RAW와 최종 코드 상단 마스크를 표시한다. well 마스크와 최종 코드 마스크는
구분한다. SDK에는 template/submit/get/list 메서드를 추가했다.

## 이번 실행 결과와 수정

전용 state: `outputs/camera-platform-design-experiments-smoke-20260927-execution`.
작업 ID: `run-bf0642fc431645fd8d17da53396b42a6`.
투명 조건·노출 1/2 ms·gain1, 선택 배율1/2, 평가 배율1.5(상하 반전), 잡음 OFF,
기본 연구 제약120 DN/SNR10/포화1%/노출4 ms로 실제 Worker가 5개 RAW를 계산했다.
`clear-e2-g1`이 선택되고 평가 Green430.140625 DN, SNR p10 27.834414,
well/최종 RAW 상단0%를 기록했다. 전체 격자 최적화의 새 결론은 아니다.

새 작업 POST는 정확히 한 번이다. 후보 선택은 평가 전에 고정됐고, 저장 결과를
주소로 다시 열 때 추가 POST/취득이 없었다. 원본 NPY 다운로드는 저장 blob hash와
일치했다. 미리보기는16×16 원본과 최종 코드 상단 마스크이며 well 마스크와 다르다.
검증 후 전용 서버·Worker/브라우저는 종료했고 DB의 결과는 유지했다.

두 실제 결함을 실패 시험으로 재현한 후 수정했다.
- `:g`의6자리 출력 때문에 서로 다른 소수 노출/gain4개가 같은 후보 ID가 되던 문제:
  정수 ID는 유지하면서 소수 설정은 왕복 가능한 표현으로 구분한다.
- 최종 CSV/결과JSON 저장 실패가 계산의 실패 처리 밖에 있던 문제: 완료한 RAW와
  고정 선택을 실패 결과에 유지한다. 별도의 실패JSON까지 저장 불가여도 기존 artifact
  참조를 Worker에 돌려준다. DB 게시까지 불가능한 장애를 해결했다는 주장은 아니다.

기존 기능의 적합 후보 없음·평가 실패 후 재선택 금지와 새 저장 실패 두 경로는
격리 서비스 시험으로 확인했다. 실제 별도 Worker/브라우저에서는 위 정상 실험 한 건을
확인했으며, 실행 중 강제취소·전원손실·설치 복구 전체를 검증한 것으로 확대하지 않는다.

저장 결과를 바로 여는 명령:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-platform/bin/python \
  tools/run_camera_design_experiments.py --serve --state-label smoke-20260927-execution
```

같은 Mac에서 `http://127.0.0.1:8798/design-experiments.html`을 열고
**실제 Worker · 1/2 ms 후보 비교**를 선택한다. `default` state는 이 결과가 없는
별도 작업실이므로 예제를 보려면 위 state-label을 유지한다.

이 정상 연결 확인을 다음 기능 대신 계속 반복하지 않는다. 다음은 동일 입력 작업실에
명시적인 움직임/채널비 비용을 연결하는 기능 확장이며, 기존의 합성 global shutter·
두 파장·색비 참조 가정을 숨기거나 기존 결정을 변경하지 않는다.

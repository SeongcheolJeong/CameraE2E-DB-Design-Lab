# Workbench transport v0.8.1

작업실은 `/api/v1/workbench`에서 이력 목록을 읽고, 선택한 작업과 모델의 원본을
필요할 때 조회한다. `/api/v1/bootstrap`과 기존 개별 원본·Export API는 유지한다.
기존 실행·모델·측정의 계산 의미, 승인 상태, 저장 형식은 변경하지 않는다.

## 계약과 모듈

- `workbench.py`: 검증된 Registry 기록을 표시용 요약으로 만든다. 대상은
  `runs`, `factory_jobs`, `replays`, `surrogates`, `models`, `surrogate_predictions`다.
  다른 Collection은 이번 버전에서 전체 문서를 반환한다. 목록을 잘라 이력을 숨기지 않는다.
- 모든 요약은 `_projection.schema = camera_workbench_projection_v1`과 Collection,
  원본 조회 경로, **전체 개별 API 기록**의 canonical SHA-256을 가진다. Job의 기존
  `content_hash`는 동결 요청의 해시이므로, 결과까지 포함하는 `record_sha256`과 다르다.
- `GET /api/v1/workbench/{collection}/{identity}`는 `camera_workbench_record_v1`,
  `record_sha256`, 변경하지 않은 `record`를 반환한다. 다른 Job 종류의 경로로 조회하면
  404를 반환한다. 원본 복사와 기존 Export에는 요약 문서를 대입하지 않는다.
- `display_summary`에는 목록·비교 그래프에 필요한 수치만 보존한다. 표시 배열은 원본에서
  그대로 가져오며 보간이나 다운샘플링을 하지 않는다. 예측 이력은 조건 수를 표시하고,
  전체 1,024개/최대 4,096개 예측은 원본 조회에서 제공한다.
- `workbench.js`: 원본 해시별 32개 항목 캐시, 중복 요청 통합, 원본 참조 검사,
  실패 시 재시도를 담당한다. 동결 문서의 해시가 다르면 거부한다. Job은 목록 조회와
  상세 조회 사이 Worker가 완료할 수 있으므로 동일 요청·전진하는 상태/시각만 허용한다.
  이 검사는 브라우저의 독립 암호학적 원본 인증을 뜻하지 않는다. 원본 SHA 계산과
  Registry·완료 감사 검사는 서버에서 수행한다.
- `app.js`: 선택한 원본을 읽기 전에는 명시적인 로딩/실패 화면을 표시한다. 늦은 응답이
  다른 화면을 덮지 않도록 렌더 세대를 확인한다. 이미 조회한 완료 상태를 오래된 목록이
  되돌리지 않도록 캐시에서 더 최신의 원본 상태를 유지한다. RAW 재처리 Source 목록은 재처리 화면을
  사용할 때 읽고, 실행 중 Poll은 대기·실행 중인 개별 Job만 조회한다.
- `GET /api/v1/session`: 버전·Workspace·State·로컬 Token만 읽는다. SDK는 이 경로를 쓰고,
  이전 서버의 404에만 기존 bootstrap으로 되돌아간다. 기존 Origin/Host 보호를 유지한다.
- 아주 작은 0이 아닌 수치는 과학 표기법으로 표시한다. 원본 JSON 값은 변경하지 않는다.

## 조회 비용과 무결성

기존 bootstrap은 같은 전체 Job 목록을 네 번 조회했고 Job마다 별도 연결/감사 조회를
수행했다. 이제 Job과 완료 감사 기록을 한 SQLite 읽기 Snapshot에서 가져와 요청·결과를
검증한 다음 종류별로 분류한다. 완료 결과와 감사 Event의 해시 검사는 생략하지 않는다.
이 버전도 전체 이력을 읽고 검증한 후 투영한다. 서버 처리 비용까지 일정하게 만드는
Pagination·증분 Index·대용량 배열 Metadata 분리는 후속 작업이다.

복제한 50개 Camera Run, 34개 Model, 4개 Factory, 2개 Replay, 1개 Surrogate 및
1,024개 조건 Prediction 이력에서 초기 응답은 11,428,652 bytes에서 845,651 bytes로
92.6% 감소했다. 상세 원본 92개를 기존 전체 API와 비교했다. 시간 측정의 실행 환경·
표본·범위는 `reports/camera_platform/workbench-http-idle-verification.json`을 따른다.
단일 호스트에서 반복한 읽기 측정이며 지속 부하 p95 또는 다른 장비의 성능 보장이 아니다.

## 검증과 남은 범위

`tests/platform/test_workbench.py`는 Job 상태 변경, 원본 동등성, 작은 Session과 Origin 경계,
SDK의 404 호환성, SQL 읽기 횟수, 감사 변조 거부, 과거 프레임 수 계약을 검증한다.
`tests/platform/browser/workbench-cache.cjs`는 요청 통합·실패 재시도·상태 역행 거부·순서가
뒤집힌 응답·캐시 용량을 검증한다. 실제 Headless UI는 원본 모달, 시간 프레임,
동일 RAW 재처리, 조회 실패와 복구, 늦은 응답 뒤 다른 화면의 입력 보존을 확인한다.

검증 과정에서 과거 Run의 `frames`가 정수인 경우를 발견해, 목록의 프레임 수가 정수인
기록과 프레임 목록인 기록을 모두 지원했다. 첫 전체 회귀 중 소스를 수정해 환경 변경
거부가 한 번 발생했으며, 해당 실행은 PASS로 세지 않았다. 소스 고정 후 전체 회귀 976개가 실패·오류·Skip 없이 통과했다. 검증 실패 기록은 `history/v0.8.1-verification-attempts`에 보존한다.

원문의 일곱 모듈·네 실행 방식과 전체 상용화 목표는 진행 중이다. 3D·일반 광학/픽셀/
전기 모델, AE/AWB/HDR/LFM, 독립 실물 검증과 전체 불확실도, 적응형 최적화·Agent,
설계 문제/의사결정 연결, 팀 권한·승인·독립 설치·지속 부하와 배포 검증은 여전히 필요하다.


## 기본 작업실 반영

v0.8.1 설치 Wheel은 SHA-256
`92590cdde42ddb630c507744e62f9fbc38bd3151975e8bc3555c3bff364d1275`이며,
111개 소스·정적 파일의 설치본 일치와 실제 HTTP를 확인했다. 173개 기존 기록의 해시,
57개 Job의 동결 요청·상태·결과 원문 bytes가 전환 전후 동일하다. 상세 근거는
`reports/camera_platform/primary-workbench-upgrade.json`에 있다.

패키지는 Checkout 밖에서 실행하지만 기존 의존성을 재사용한다. 깨끗한 OS 설치나
다른 Schema의 DB Migration, 전체 상용 출시 Gate 완료를 뜻하지 않는다.

직접 RAW 재처리 버튼은 이미 조회한 실행의 `raw_frames.npy` 산출물로 활성 여부를
판단하며, 클릭 시 원본 목록을 새로 검사한다. 아직 읽지 않은 Source 목록을 근거로
지원되는 버튼을 비활성화하던 첫 설치의 회귀를 수정하고 전용 UI 검사를 추가했다.

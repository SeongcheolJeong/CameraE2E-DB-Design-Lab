# 다음 구현: 일반 모델 근거를 유지하는 ISP 탐색과 증거 조회

이 문서는 `0.16.0.dev8` 다음 구현의 설계안이다. 아직 구현된 기능으로 표시하지 않는다.
큰 광학·픽셀 모델을 실제 ISP 후보 탐색에 사용할 때 생기는 저장·검증 병목을 해결한다.
일곱 모듈·네 실행 방식, 일반 광학·픽셀·전기 모델 및 HDR/LFM 확장 목표는 유지한다.

## 확인된 문제와 확인하지 않은 부분

근거는 [성능 감사](../reports/camera_platform/charge-collection-v016/performance-audit.json),
[4후보 거부 기록](../reports/camera_platform/charge-collection-v016/http/four-candidate-plan-result.json),
[2후보 Study](../reports/camera_platform/charge-collection-v016/http/study.json)다.

- `4×16×16` 원본 RAW는 4,224 bytes다. 보존 calibration은 7,648,587 bytes,
  ISP replay 요청과 결과는 합계 28,166,139 bytes다. 모델·원본·보정 snapshot이 중첩된다.
- 기준 후보와 denoise 강도 `[0, .4, .8]`의 4후보 계획은 20.912초 뒤 256 MiB
  archive 한도로 거부됐다. 기준 후보와 `[0]`의 2후보는 실행됐지만, 계획의 bundle
  추정은 225,624,258 bytes이고 실제 계산 working-array 추정은 675,236 bytes다.
- `api.artifact()`는 매번 `registry.list_runs()`를 먼저 호출한다. 이 함수는 모든
  요청·결과 JSON을 해석하고 canonical hash와 완료 증거를 확인한다. 당시 11개 job의
  JSON은 70,595,884 bytes였다. 대상 artifact 크기와 무관한 전체 이력 비용이다.
- `isp_studies.template()`는 두 축을 항상 제시하지만, source-bound ISP 기본값은
  sharpen을 bypass한다. `_prepare()`가 이 축을 거부하므로 기본 양식 자체를 고쳐야 한다.
- Study 계획의 후보 루프는 매번 `prepare_replay → resolve_for_source → _source_context
  → verify_models`를 호출한다. 모델·Factory 검증과 snapshot 직렬화가 반복된다.

위 크기·응답·코드 경로는 확인했다. CPU 시간 중 각 함수의 비중은 측정하지 않았다.
API CPU 100.1%, RSS 933,392 KiB와 Worker 0.1%를 한 시점에 관찰했으나 물리 엔진
오류의 근거가 아니다. 먼저 paired benchmark로 개선량을 측정한다.

## 1. 실행 가능한 기본 탐색 축

`ISPStudyPlan`의 기존 입력 계약과 `_prepare()`의 bypass 거부를 유지한다.
`template()`는 `resolve_isp(..., allow_v2=True)`로 실제 고정 baseline을 확인하고,
`mode != "bypass"`인 `raw_denoise.strength`, `linear_sharpen.amount`만 만든다.
baseline의 calibration ID도 요청과 같아야 한다. 기본값 때문에 ISP stage를 켜거나
보정 ID를 바꾸지 않는다. baseline 후보는 기존처럼 항상 포함하고 profile hash로 중복 제거한다.

Workbench에는 같은 응답에서 얻은 허용 축만 표시하고, JSON 편집의 최종 판정은 서버가 한다.
둘 다 bypass라면 유효하지 않은 빈 tuning 계획을 반환하지 않는다. template 요청을 명확한
도메인 오류로 거부하고, baseline의 적용 단계를 먼저 설정하도록 표시한다.
독립 검증의 `axes=[]`, 선택·부모 Study 고정, 데이터 중복 거부는 그대로 둔다.

수정 위치: `isp_studies.py`, `static/isp-studies.js`, template API/SDK 응답 처리.
두 단계의 on/off 네 조합, 활성 baseline 값과 후보 중복, bypass 수동 JSON 거부를 검증한다.

## 2. 불변 snapshot을 한 번만 보존하는 객체 그래프

기존 v1 record·job·case·audit bytes와 ID/hash는 재작성하지 않는다. 새 Study·replay의
저장 envelope 및 export에 별도 v2를 도입하고, reader는 v1 inline과 v2 참조를 명시적으로
분기한다. 사용자가 제출하는 `camera_isp_study_v1` 조건 계약은 바꾸지 않아도 된다.
기존 일반 API 응답에서 snapshot을 몰래 ID만으로 바꾸지 않는다.

새 `evidence_objects.py`에 canonical 객체와 명시적인 참조를 둔다. 제안 참조 필드는
`schema`, `sha256`, `bytes`, `semantic_sha256`, `record_kind`, `record_id`다.

- `sha256`: 참조를 포함한 저장 node의 정확한 canonical bytes 해시.
- `semantic_sha256`: 모든 child를 복원한 논리 객체의 기존 `digest()` 값.
- `record_kind`/`record_id`: 원본의 정확한 식별자이며, 임의의 최신 record를 선택하는 키가 아니다.
- 알려진 typed snapshot 필드만 참조로 바꾼다. 사용자 payload 속 같은 이름의 dict를
  휴리스틱으로 해석하지 않는다. RAW·NPY·Factory response artifact descriptor는 유지한다.

공통 source, model, calibration, Factory job, selection/analysis를 DAG node로 보존한다.
node 내부의 반복 snapshot도 같은 규칙으로 분리한다. 단순히 큰 최상위 record를 blob으로
옮기고 내부 중복을 그대로 두는 것으로 끝내지 않는다. 작은 root에는 참조와 실험별 조건만 둔다.

검증 reader는 각 public operation에서 대상 root, 전체 도달 가능한 node와 artifact bytes,
원본 registry binding, Factory 부모 관계, job request/result 및 완료 audit 증거를 새로 확인한다.
한 operation 안에서만 검증한 node·source context를 재사용한다. service 전역 cache나
파일 크기·mtime만으로 무결성을 대신하지 않는다. 반환 객체는 분리하거나 변경 불가능하게 한다.
누락·순환·잘못된 schema·길이·물리 hash·semantic hash·원본 binding 오류를 거부하고,
node 수·깊이·복원 bytes 한도를 allocation 전에 적용한다.

계획 단계는 `_DraftRegistry`처럼 메모리에서 graph와 정확한 예상 bytes만 만든다.
submit은 계획을 다시 검증하고 blob을 먼저 확정한 뒤 기존 원자적 transaction에서 root,
후보, 모든 cell·audit를 게시한다. rollback 후 생긴 미참조 blob은 공개하지 않으며, 이번
범위에서 기존 blob 삭제나 자동 garbage collection은 하지 않는다.

새 export는 root와 도달 가능한 `objects/<sha256>.json`, 필요한 `artifacts/<sha256>/...`를
각각 한 번 포함한다. closure의 model/Factory/calibration/acquisition/reference/selection
관계를 전부 오프라인으로 복원하고 검증할 수 있어야 한다. UI 조회와 bundle은 큰 논리 객체를
여러 번 펼치지 않고 verified graph를 공유한다. v1 export는 기존 canonical 내용과 hash를
그대로 유지한다. v2를 펼친 호환 export가 한도를 넘으면 명시적으로 거부한다.

현재 `_artifacts()`는 일반 dict/list만 재귀 순회하므로 v2 closure traversal을 별도로
구현해야 한다. 참조가 보인다는 이유로 증거 검사를 건너뛰면 안 된다. case review의 기존
requirement/Test 대응, selection의 analysis hash, calibration fitting ancestry·RAW 중복
검사는 복원된 논리 객체에 계속 적용한다. 새 v2 case envelope가 필요하면 버전을 명시한다.

archive 사전 계산은 실제 root + 고유 node + 고유 artifact + 예상 output을 계수한다.
256 MiB 상한을 올리거나 후보를 자동 축소해 문제를 숨기지 않는다. 계산 working-array 한도와
archive 크기는 별도 항목으로 표시한다.

## 3. Artifact 소유자 인덱스와 대상 검증

registry schema migration으로 `artifact_owners` 인덱스를 추가한다. 제안 행은 artifact
SHA, owner 종류·ID, typed descriptor 위치, descriptor hash다. 한 SHA가 여러 owner나
파일 이름을 가질 수 있으므로 SHA 단일 행으로 덮어쓰지 않는다. 인덱스는 lookup 보조 자료이며
진실의 원본이 아니다.

artifact GET은 인덱스로 후보 owner를 찾고, 그 owner record/request/result와 필요한 완료
증거를 새로 검증한 다음 정확한 descriptor binding 및 blob SHA/bytes를 확인한다.
직접 등록된 native response라면 현재처럼 해당 모델과 Factory authority도 확인한다.
파일 이름·media type 충돌 정책은 기존 owner 우선순위를 명시적으로 보존한다.
무관한 job 손상 때문에 정상 대상 artifact를 막지 않되, 손상된 대상 owner를 다른 owner로
몰래 대체하지 않는다. 전체 감사는 별도 전체 검증 작업으로 남긴다.

새 record 게시·job 완료와 owner 인덱스 갱신은 같은 transaction에 둔다. 기존 DB migration은
owner 원본을 검증하며 한 번만 backfill하고 schema version을 원자적으로 올린다. restart에
안전해야 하며 손상된 owner를 성공적으로 이관했다고 표시하지 않는다. request마다 전체 이력을
스캔하는 fallback을 두지 않는다. v1 owner도 같은 인덱스와 검증 경로를 사용한다.

수정 위치: `registry.py`, `api.py`, job 완료·record publication 경로,
`isp_study_registry.py`, 신규 객체 reader/export, backup/restore와 schema version 검사.
기존 immutable trigger, queue 소유권·취소, idempotent submit, 완료 audit hash는 유지한다.

## 검증과 완료 조건

다음은 구현 후 만족시킬 acceptance 조건이며 현재 달성했다는 뜻이 아니다.

| 대상 | 고정 fixture와 통과 조건 |
|---|---|
| 2후보·4후보 실제 실행 | 현재 dev8의 native v3 Factory Lens + generation/collection + `4×16×16` RAW·동일 calibration·동일 flat ROI를 사용한다. baseline+.0 및 baseline+[.0,.4,.8] 모두 plan→submit→Worker→analysis→selection→case→bundle→restart가 성공하고 완전한 closure를 복원한다. |
| 증거 동등성 | 동일 후보의 RAW·ISP 수치·metrics는 기존 경로와 일치한다. 저장 버전/새 환경에 따른 ID 차이는 분리하고, 기존 v1 record·export·case bytes와 hash는 정확히 보존한다. |
| 크기·시간 | 4후보가 기존 256 MiB 제한 안에서 성공해야 한다. 같은 원본·새 격리 DB의 전후 plan/read/bundle latency, 응답·DB·bundle bytes, peak RSS를 기록한다. 성공을 위해 원본 해상도·Factory 표본·증거·후보 수를 줄이지 않는다. |
| 조회 확장성 | 같은 target artifact와 owner를 고정하고 무관한 history를 10/100/1000 jobs 및 작은/큰 JSON으로 늘린다. owner 해석·검증 횟수와 대상 외 JSON decode bytes가 증가하지 않아야 한다. 인덱스 탐색 자체의 log 규모와 target blob 해시 비용은 구분한다. |
| 손상·이관 | 읽기 사이의 같은 크기 blob 변경, node/원본 record/request/result/audit/descriptor/index 위변조·삭제를 주입한다. 대상 검증은 실패해야 하고 새 operation이 손상을 놓치면 안 된다. v1 DB backfill·중단·재시작·중복 실행·backup/restore를 검증한다. |
| 상태 변화 | queued→partial→complete를 다음 public read가 관찰한다. 후보별 metrics, selection hash, 취소와 cache 재사용의 조건을 그대로 검증한다. |

기존 `test_isp_evidence_reads.py`, `test_isp_studies.py`, `test_isp_study_api.py`,
case·backup·registry 무결성 검사를 확장한다. content hash만 맞는 누락된 closure, 순환 참조,
reference/fitting ancestry의 재명명, raw frame 중복, calibration 적용 중복도 포함한다.
순수 함수나 mock 통과만으로 끝내지 않고 위 2/4후보를 실제 API·별도 Worker로 실행한다.

## 구현 순서와 남은 선택

1. 기본 축과 UI를 수정하고 v1 regression fixture·현재 성능 측정을 고정한다.
2. artifact owner 인덱스·migration·fresh verification을 독립 구현한다.
3. typed 객체 graph와 dual reader, Study 준비의 operation-local 검증 context를 구현한다.
4. replay·Study·case·export·backup의 v2 쓰기를 연결한 뒤 2/4후보 통합 검증을 완료한다.

snapshot이 반복된 것이 크기 증가의 주요 원인이라는 판단은 저장 필드 크기에 근거한다.
개선 latency/RSS 목표 수치는 아직 측정 전이므로 임의의 보장을 만들지 않는다. DAG의 구체적
node 경계, 한 operation의 depth/node/byte 상한, v2 API 선택 방식은 구현 시작 시 짧은
계약 fixture로 확정한다. 기존 hash를 바꾸거나 closure를 버리는 선택지는 허용하지 않는다.
이 범위는 물리 센서 검증이나 일반 TCAD/HDR/LFM 모델 완성을 대신하지 않는다.

# ISP 후보 탐색의 근거 저장·조회 확장 · v0.16.0.dev9

일반 광학·픽셀 모델의 큰 근거를 RAW 재처리와 후보 탐색에서 재사용한다. 기존 기록의
논리 내용·ID·해시·감사 이력을 유지하며, 새 대형 JSON은 중복 부분을 한 번만 저장한다.
이 기능은 모델 정확도나 실물 카메라 검증 상태를 바꾸지 않는다.

## 실행 가능한 기본 탐색 축

Study template은 고정한 v2 ISP와 calibration ID가 일치하는지 확인하고, 실제 활성화된
`raw_denoise.strength`, `linear_sharpen.amount`만 제안한다. 두 단계 모두 bypass이면
설정을 먼저 지정하도록 오류를 반환한다. UI의 변수 선택·추가도 이 목록을 따른다.
입력한 프로파일을 자동 활성화하지 않으며, 수동 JSON의 bypass 축은 계속 거부한다.
독립 검증에서는 저장된 선택과 빈 탐색 축을 유지한다.

## 논리 JSON과 물리 저장의 분리

`evidence_objects.pack()`은 JSON container를 명시적인 node로 인코딩한다. node는 literal
`value`, 직접 자식의 위치와 reference를 담는 `children`, 버전 schema로 구성된다.
사용자 payload가 reference처럼 생겼더라도 literal로 보존한다. 일반 payload의 키 이름을
보고 임의로 참조로 해석하지 않는다. 기본 분할 기준은 1 KiB이며, 같은 node bytes는 같은
SHA를 사용하므로 여러 후보의 source/model/calibration이 같은 저장 객체를 공유한다.

Reference에는 node SHA·bytes와 원래 JSON의 `semantic_sha256`·`expanded_bytes`가 있다.
전자는 저장된 물리 객체를, 후자는 복원된 정확한 canonical JSON을 검증한다. 깊이·node 수·
물리 크기·논리 확장 크기를 제한하고 누락·중복 위치·잘못된 경로·순환·SHA 변경을 거부한다.
복원한 반복 객체는 각각 독립적으로 수정할 수 있으며, 전역 신뢰 cache는 없다.

Registry의 새 256 KiB 이상 JSON은 JSON 바깥의 명시적 prefix와 root reference로 저장한다.
Graph node는 기존 SHA blob 저장소에 놓지만, 그 자체를 공개 artifact로 등록하지 않는다.
작은 새 JSON은 기존 inline 형식을 사용한다. 새 writer는 요청·결과·보정·Study·case 등의
큰 snapshot에 같은 방식을 적용하며, public API는 기존의 완전한 논리 JSON을 반환한다.
`content_hash`, request hash, result hash와 audit는 원래 논리 JSON을 계속 가리킨다.
기존 v1 record/job/audit bytes는 이관 시 다시 쓰지 않는다.

## 조회와 무결성

Registry schema 2는 artifact 소유자와 선호 순서 인덱스를 추가한다. 기존 schema 1은
원본을 확인하여 transaction 안에서 한 번 backfill한다. 이전 읽기 순서를 유지하고,
GET은 선택된 소유자·감사 근거·descriptor binding·현재 blob SHA/bytes를 검증한다.
다른 작업의 JSON 전체를 해석하지 않는다. 인덱스는 권한이나 원본의 대체물이 아니다.
선택된 행 또는 guard가 손상되면 다른 소유자로 넘어가지 않으며, 후속 정상 등록이
손상된 guard를 조용히 정상화하지 않도록 검사한다.

Study 계획과 복합 읽기는 한 operation에서 검증한 record/job을 재사용한다. 반환 객체는
분리된 복사본이며, 다음 operation은 새로 검증한다. 파일 경로는 검증된 bytes snapshot이
아니므로 blob 경로를 재사용해 해시 검사를 생략하지 않는다. 계획은 계속 읽기 전용이다.

대형 JSON hash·graph 인코딩과 원본 해석은 SQLite 쓰기 transaction 전에 수행한다.
Transaction 안에서는 읽었던 행의 저장 bytes·상태·목록이 그대로인지 다시 확인하고,
기록·전체 작업 묶음·artifact index·audit를 함께 게시한다. 변경된 입력은 다시 확인하며,
사례의 최신 revision과 queue capacity 검사를 생략하지 않는다. 실패한 게시 준비에서
생긴 미참조 immutable blob은 공개 근거나 backup 포함 대상으로 승격하지 않는다.

Worker는 일시적인 `SQLITE_BUSY`만 짧은 연결 대기와 제한된 재시도로 처리한다.
작업을 가져간 뒤 추가 DB 조회 실패로 다음 작업까지 가져가지 않으며, 결과 저장 재시도는
같은 계산 결과를 사용한다. 다른 DB 오류·재시도 기한 초과는 실패로 남긴다. 대기 중 종료
요청을 처리하지만, 강제 종료에도 미게시 결과를 보장하는 별도 durable journal은 없다.

Backup은 원본 record·job·audit와 공개 artifact뿐 아니라 참조한 private graph node도
완전하게 검증·보존한다. 복원은 새 디렉터리에만 수행한다. 기존 v1 backup을 읽을 수 있으며,
새 schema 2 상태는 이를 지원하는 runtime으로 열어야 한다. 설치본 v0.15의 상태를
개발 runtime으로 직접 열어 이관하지 않는다. 현재 배포 전환은 수행하지 않았다.

## 새 Study의 근거 묶음

새 Study는 `evidence_format=camera_evidence_bundle_v2`를 고정한다. 예상 archive 크기는
중복되지 않는 graph object·원본 artifact와 예상 결과를 기준으로 계산하며, 실제 export도
기존 **256 MiB** 한도를 유지한다. 논리 JSON 복원 한도는 별도 **1 GiB**이다.
기존 Study의 v1 bundle은 기존 inline manifest와 이름별 artifact 경로를 유지한다.
일반 run/replay/release bundle 형식도 이번 변경에서 바꾸지 않았다.

새 ZIP은 `manifest.json`, `objects/<sha>.json`, `artifacts/<sha>`, 설명 파일로 구성된다.
같은 bytes의 여러 artifact 이름은 논리 manifest에 그대로 남고 bytes는 한 번만 들어간다.
아래 reader는 object·artifact의 크기/해시·완전한 포함 관계를 확인하고 원래 JSON을 복원한다.

```python
from camera_platform.evidence_package import read_bundle
from camera_platform.registry import digest

# verified_export는 public export가 원본·Factory·보정·완료 근거를 검증해 반환한 결과다.
expected = digest(verified_export)
restored = read_bundle("study.zip", expected_manifest_sha256=expected)
assert restored == verified_export
```

독립적으로 보관한 기대 hash를 제공하면 검증한 export와 정확히 같은 내용임을 확인한다.
외부에 고정한 hash 없이 ZIP 내부 hash만 확인하는 것은 bytes 정합성 확인이며 출처 인증이나
실물 검증을 새로 성립시키지 않는다. Domain API의 원본 binding 검사는 계속 적용한다.

## 검증과 남은 범위

최종 실행·재시작·회귀 결과는
[개발 근거](../reports/camera_platform/evidence-scaling-v016/evidence-summary.json)에 기록한다.
이전 v0.16.0.dev8의 같은 16×16×4 RAW, 보정, ISP를 사용하여 baseline+[0,.4,.8]의
4후보를 실제 Worker에서 실행하고, 일치하는 기존 후보의 수치 결과·graph 복원·사례 연결을
확인하는 것이 이번 실제 실행 기준이다. 이전 상태는 보존하고 복제한 QA에서만 이관한다.

논리 JSON API 응답을 작게 투영하는 기능은 추가되지 않았으므로 큰 전체 case/export 응답의
직렬화·전송 비용은 남는다. Graph를 저장한다고 그 비용까지 사라지는 것은 아니다.
일반 광학·전하 수송·회로 비선형, HDR/LFM의 움직임 정합, 시간 ISP·적응형 최적화와
독립 실물 검증은 계속 별도 개발 범위다.
사용자 우선순위에 따른 다음 물리 확장은
[비선형 전하 응답과 RAW 역보정](camera-platform-nonlinear-readout-next-slice.md)이다.

## 최종 소스에서 확인한 결과와 한계

같은 보존 RAW **4×16×16**, calibration, 기준 ISP로 4후보를 실제 API·Worker에서 실행했다.
네 작업이 모두 성공했고, 기존 baseline·denoise 0 후보와 비교한 **60개 NPY 배열 및 수치 지표가
동일**했다. 실행 환경·구현 버전의 차이는 별도로 기록했다. 선택·사례 게시 후에도 동일 Worker가
살아 있었으며, 기존 기록과 v1 bundle manifest는 그대로 유지됐다.
[HTTP 실행 근거](../reports/camera_platform/evidence-scaling-v016/http-final/http-verification.json)

새 bundle은 전체 Factory·전하 생성/수집·원본·보정 근거를 정확히 복원했다. 논리 manifest
**191,276,531 bytes**가 **168개 graph object·157개 고유 artifact**, 압축 전 **49,779,887 bytes**,
ZIP **14,390,510 bytes**로 보존됐다. 최종 플랫폼 회귀 검사(`tests/platform`)는 Python **2,876개**, Node **83개** 통과이며,
별도 집중 검사 수를 중복 합산하지 않았다.
[검사 집계](../reports/camera_platform/evidence-scaling-v016/test-summary.json)

실제 backup/restore도 **44개 record·17개 job·95개 audit·457개 blob**을 보존했다.
그중 private graph node **186개**는 공개 artifact로 승격되지 않았고, 원본과 복원된 DB 행의
해시가 일치했다. 복원 과정은 Worker를 시작하지 않았다.
[복원 근거](../reports/camera_platform/evidence-scaling-v016/backup-restore-verification.json)

**용량 추정과 대기 시간에는 해결할 문제가 남아 있다.** 계획은 압축 전 **24,917,593 bytes**를
예상했으나 실제는 **49,779,887 bytes**였다. 후보별 `summary.json` 합계 **30,682,011 bytes**에
비해 완료·지표 metadata 예약은 **2,097,152 bytes**다. 실제 export의 256 MiB 검사는 유지되고
이번 네 후보는 통과했지만, 더 큰 계획을 사전에 안전하게 허용한다는 보장은 없다.
[용량 감사](../reports/camera_platform/evidence-scaling-v016/http-final/bundle-estimate-audit.json)

최종 case POST는 **231.48초** 걸렸다. Workbench 초기 진입과 저장된 Study 로딩도 수분 동안
대기하는 현상이 관찰되어 **UI 응답성은 수용하지 않았다**. 동시 검사·UI·backup 부하가 있는
측정이므로 처리 속도가 개선됐다는 비교 근거로 사용하지 않는다. 이번 결과는 고정된 네 후보의
계산·보존 경로 검증이며, 일반 규모의 사용성·실물 카메라 검증·고객 배포 승인을 뜻하지 않는다.
[남은 문제](../reports/camera_platform/evidence-scaling-v016/known-issues.json)

서버·Worker를 재시작한 뒤 전체 DB 원본·파생 행, 457개 blob의 SHA/bytes, 152개 Python 소스와
환경을 다시 비교했고 실제 Study·analysis HTTP 응답도 이전 결과와 일치했다. 재시작 검사에서는
계산·case POST·bundle 생성을 반복하지 않았다.
[재시작 근거](../reports/camera_platform/evidence-scaling-v016/restart-verification.json)

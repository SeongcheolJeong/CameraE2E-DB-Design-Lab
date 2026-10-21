# v0.10.0 · RAW 응답 취득, 공통 관측, 분석 버전

기본 작업실에 **v0.10.0을 설치해 실행 중**이다. Validation에서 원본 RAW와 취득 로그를
가져오고, 같은 원본을 여러 응답 지표로 분석하며, 해당 Test로 카메라 계산·비교를 수행한다.
기존 v0.9.0의 기록 182개·작업 60개·감사 이벤트 362개를 보존했다.
이는 로컬 엔지니어링 기능 인도이며, 전체 상용화 Goal은 계속 진행 중이다.

## 작업실에서 사용하는 순서

1. **실측 검증 → 폐루프 RAW·제어 로그 가져오기**에서 등록한 촬영 프로그램과 공통 시험을 선택한다.
   첫 관측 시험이 없으면 아홉 프레임별 지표 중 하나로 형식을 불러와 이름·허용 오차·검증
   종류를 검토하고 등록한다. 장면·정책·프로그램·Native RAW 형상은 고정한다.
2. 정수 RGGB RAW NPY `[N,H,W]`와 취득 메타데이터 JSON을 선택한다. 파일 크기 한도는
   RAW 64 MiB, UTF-8 JSON 1 MiB다. JSON은 편집하지 않으면 파일 바이트를 그대로 전송한다.
   편집한 경우 편집된 UTF-8 JSON을 새 원문으로 보존한다.
3. 실제 RAW 대응 인덱스·적용 노출/Gain/WB·프레임 시작/적용 시각·명령 로그·시계 대응과
   출처를 확인하고 **원본 보존 · 첫 분석 생성**을 실행한다. 저장은 취득·분석·Measurement가
   함께 공개되는 단일 트랜잭션이다. 불일치·원본 중복·불완전한 필수 출처는 거부한다.
4. **보존 RAW를 다른 지표로 분석**에서 다른 Test와 분석 버전을 만든다. 궤적·구간 최대 목표
   오차·표본 정착 시간을 지원한다. 지표별 불확실도를 별도로 선언하고 같은 취득 식별자를 유지한다.
5. 분석 이력에서 **이 분석으로 비교**를 선택한다. 그 Test로 새 카메라 계산을 실행하고
   같은 Test/Measurement로 비교한다. 분석 지표를 바꾸면 이전 비교는 표시하지 않는다.

빈 취득 형식은 등록 가능한 실측 예제가 아니다. 출처·라이선스·데이터 ID·실행/정합 근거는
비어 있고, 실제 RAW 매핑·설정·시각은 `null`이다. 정합 경계도 미확립이다.
실제 기록으로 채워야 하며, 명령 로그가 없는 경우와 명령이 발행되지 않은 경우를 구분한다.
`commands: []`는 발행 명령이 없다는 선언이다. 실측에는 Serial·장비 ID·교정 버전·정책 실행
근거가 필요하다. 시뮬레이션 원본의 이름이나 dtype만 바꿔 실측으로 등록할 수 없다.

## API와 원문 보존

- `POST /api/v1/control_response_tests/template`: 촬영 프로그램에 대응하는 관측 Test 형식.
- `GET /api/v1/control_response_tests/{test_id}/acquisition-template`: 미확인을 보존하는 취득 형식.
- `POST /api/v1/captures/control-response/import`: multipart `raw`와 `metadata_file`을 사용한다.
  기존 `metadata` 문자열도 지원하지만 두 메타데이터 입력 중 정확히 하나만 제공해야 한다.
- `POST /api/v1/capture_analyses`: 원본 취득을 참조하는 새로운 관측 버전.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8770") as client:
    result = client.import_control_response_file("acquisition.json", "raw.npy")
    capture_id = result["capture"]["id"]
    measurement_id = result["measurement"]["id"]
```

브라우저는 JSON을 File/Blob 파트로 전송한다. UTF-8·공백·LF/CRLF 줄바꿈은 원본 Blob과
해시로 보존하며, 엄격한 JSON 계약 검사를 별도로 적용한다. 검증 시 원본 RAW와 로그를
재생해 분석 기록/Measurement와 일치하는지 확인한다. 자세한 경계는
[공통 관측](camera-platform-control-observation.md)과
[취득/분석 분리](camera-platform-capture-analysis.md)를 따른다.

## 인도와 검증 근거

- [플랫폼 회귀 시험](../reports/camera_platform/response-v010-tests.xml): 1,089개 통과.
  [런타임 전환 경합 검사](../reports/camera_platform/response-transition-tests.xml): 2개 통과.
- [설치 패키지](../reports/camera_platform/package-verification.json): Python/static 파일 123개와
  설치본 HTTP 바이트 일치, 세 Solver·공간/시간/폐루프 촬영·재처리·설계 탐색·Surrogate·응답
  원문 업로드·다중 분석 실행을 확인했다. 기존 pyisetcam 엔진 Python은 v0.9.0과 같다.
- [개발 폴더 밖 실행·재시작](../reports/camera_platform/response-installed-runtime-verification.json):
  9항목 통과. 깨진 개발 코드와 개발 UI가 설치본을 덮지 못하며 실제 Worker가 계산한다.
  개발 폴더 변경/재시작 뒤 응답 RAW 해시와 취득·분석 재생이 유지된다.
- 설치본 브라우저: [새 업로드 흐름](../reports/camera_platform/response-upload-ui/installed/verification.json)
  20항목, [다중 분석과 재계산](../reports/camera_platform/capture-analysis-ui/installed-v010/verification.json)
  21항목 통과. 기본 작업실 읽기 전용 검사는 새 응답 15·Workbench 29·기존 제어 26·시간 31항목이다.
- [기본 작업실 전환](../reports/camera_platform/primary-response-upgrade.json)은 기존 기록/작업/
  감사 이벤트 해시를 보존했다. [기존 시간 촬영 재계산](../reports/camera_platform/primary-response-legacy-rerun.json)은
  RAW/RGB 해시가 모두 동일하다.
- [기본 작업실 예제](../reports/camera_platform/primary-response-verification.json)는 **합성 데이터**다.
  명목 잔차·분석 계보를 확인했고, [원본 포함 근거 ZIP](../reports/camera_platform/primary-response-evidence.zip)은
  물리 검증 미확립에 따른 차단 상태를 유지한다.

Wheel SHA-256: `2af0e3ef73b1cd12c4b97ebfbc00d2dea7ae49fdb1cbfd261c9fb74fa12699bd`.
이전 패키지와 인도 기록은 `reports/camera_platform/release-archives/v0.9.0/`에 보존했다.
설치 환경은 기존 Python 의존성을 재사용한다. 깨끗한 OS 설치·의존성 완전 격리의 증거는 아니다.
배포물 이름은 기존 `pyisetcam 0.1.0`이며 플랫폼 버전/환경/Wheel 해시로 실제 인도를 구분한다.

## 운영과 다음 범위

`tools/upgrade_camera_platform_runtime.py`는 기본적으로 전환 근거·현재 런타임·유휴 상태만
검사한다. `--apply`는 구 버전 코드로 백업하고 서버 종료 후 상태를 다시 확인한 다음
새 설치본을 선택한다. 종료 직전 들어온 작업이 있으면 구 버전으로 재개한다. 새 API가 정상
서비스를 시작한 뒤 들어온 새 작업은 보존하고, 기존 기록/작업/감사 이벤트 해시가 유지되는지
검사한다. 실패 시 코드 선택을 복구하며 DB를 백업으로 덮어쓰지 않는다.
새 형식의 데이터 생성 후 임의의 구 버전으로 돌아가는 일반 Migration/복구 정책은 후속 범위다.

독립 실측과 장비 취득 Adapter, 전체 응답/모델/수치/표본/타이밍 불확실도는 미확립이다.
이번 인도로 원문의 일반 3D 장면·재질/광원·광학/픽셀/전기 모델·HDR/LFM·고급 최적화·사례/
결정/Agent·역할/승인·고객 환경 운영까지 완료했다고 계산하지 않는다. 다음 개발은 전체
요구사항 감사에서 큰 미완료 모듈을 다시 선정하며, 응답 UI의 작은 확장만 반복하지 않는다.

# 장면별 카메라 준비

**v0.12.0에서 도입되어 현재 기본 설치본 v0.13.0에도 포함된 기능입니다.** v0.12 당시 [설치 전환](../reports/camera_platform/primary-preparation-upgrade.json)은 기존 208개 기록·68개 작업·412개 감사 이벤트를 보존했고, [기본 작업실의 준비 → Mitsuba → RAW/RGB 실행](../reports/camera_platform/primary-preparation-verification.json) 7항목을 통과했습니다. 전체 Camera Simulation & Validation 플랫폼 Goal의 Camera Composer–Scene Runtime 연결을 확장하며 전체 상용화 완료를 뜻하지 않습니다.

**후속 인도 v0.13.0:** 준비된 카메라를 [저장 분광 복사휘도 재사용](camera-platform-scene-projection.md)에 연결했습니다. 원본 장면의 시점·초점거리·Native 격자·파장과 전역 조건을 유지할 때 새 투과율/조리개와 대상 PSF·Sensor·ISP를 적용합니다. 카메라 준비의 `ready`만으로 광휘 재사용을 허용하지 않으며 원본 렌더링과의 추가 검사를 거칩니다. [개발 회귀 1,216개](../reports/camera_platform/projection-development-tests.xml), [v0.13 설치본 격리·재시작 17항목](../reports/camera_platform/projection-installed-runtime-verification.json), 설치 QA의 [카메라 준비 20항목](../reports/camera_platform/projection-ui/preparation-v013/verification.json)과 [광휘 재사용 26항목](../reports/camera_platform/projection-ui/installed-v013/verification.json)을 확인했습니다. [기본 v0.13 전환](../reports/camera_platform/primary-projection-upgrade.json)은 기존 212개 기록·71개 작업·425개 감사 이벤트를 보존했고 [실제 재사용 실행 9항목](../reports/camera_platform/primary-projection-verification.json)을 통과했습니다. 기본 작업실의 [준비 화면 23항목](../reports/camera_platform/projection-ui/primary-prepared/verification.json)과 [전체 7개 흐름·168항목](camera-platform-installed-runtime.md#기본-작업실의-화면-검증)도 통과했습니다(POST 0회·브라우저 오류 0건). 개발·전환·패키지의 정확한 범위는 [새 기능의 근거 표](camera-platform-scene-projection.md#검증-근거와-인도-상태)를 따릅니다.

장면 화면에서 **이 장면의 새 카메라 준비**를 선택하면 기존 구성의 Lens·Sensor·ISP를 출발점으로 새 구성 초안을 만듭니다. 선택 장면의 정확한 조명 ID와 Lens가 이미 선언한 조리개만 초안에 반영합니다. 사용자는 모델 버전, 노출, Gain, 온도, 조리개 및 고급 계약을 검토·편집합니다. 원래 구성과 모델은 변경하지 않습니다.

**준비 조건 검사**는 다음을 저장 없이 확인합니다.

- 한 개씩 고정된 Lens·Sensor·ISP, 기존 Composer의 파장·입사각·유효 영역·RAW·효과 중복 계약
- 장면의 고정 조명 ID, 재질·광원 파장 범위, 정확한 Sensor 파장과 Native 격자
- Lens에 선언된 초점거리와 F-number 및 촬영 조리개의 일치
- PSF와 Sensor의 정확한 파장·픽셀 Pitch, 해석 가능한 ISP 프로필
- 실제 Spatial Runtime이 요구하는 Lens/Sensor 응답 파장 좌표와 각도 범위
- 고정 Sensor Gain과 촬영 조건, 실제 Readout의 bit depth·black level 및 지원 RAW 형식
- Factory 응답이 고정한 기존 광학·환경·촬영 조건

실제 `SpatialSceneRuntime.validate_request`를 최소 8×8 crop·1 frame의 영 광량 입력으로 호출합니다. 전체 Native 프레임을 할당하거나 계산하지 않고, 이 검사 입력을 Scene이나 측정 근거로 저장하지 않습니다. 실제 장면의 광량·전하 상한과 렌더링 부하는 실제 입력을 얻은 뒤 해당 실행 단계에서 다시 검사합니다.

불명확하거나 맞지 않는 조건은 사유와 함께 표시하며 저장할 수 없습니다. 초점거리·F-number·분광 투과율을 임의로 보충하거나 Factory 응답의 적용 조건을 변경하지 않습니다. 다른 조명으로 옮길 수 없는 고정 응답은 새 응답 생성/등록 작업이 필요합니다. 이 기능은 일반 광학 모델 생성이나 자동 보정 기능을 대신하지 않습니다.

검사 뒤 초안을 바꾸면 저장이 다시 비활성화됩니다. 서버도 전체 초안·원본 해시·실행 환경을 다시 검사하고 미리보기 해시와 일치할 때만 새 Assembly 하나를 원자적으로 저장합니다. 같은 요청을 반복하거나 동시에 재시도해도 동일한 Assembly를 반환합니다. 저장 기록은 기준 구성·선택 Scene·각 모델의 해시와 조건 변경 내역을 포함합니다.

저장된 구성은 장면의 렌더링 입력으로 연결됩니다. 작은 Sensor에는 Native 범위 안의 짝수 crop을 제안합니다. 분광 투과율은 여전히 비어 있으며 실제 입력의 출처·방법과 함께 명시해야 합니다. 그 뒤 기존 Mitsuba → PSF → 실제 RAW/RGB → 원본 근거 ZIP 경로를 사용합니다.

## 공통 API와 SDK

| 호출 | 동작 |
| --- | --- |
| `GET /api/v1/scene_authoring/camera-template?scene_id=…&assembly_id=…` | 고정 모델과 장면 조건에서 새 초안 작성; 저장 없음 |
| `POST /api/v1/scene_authoring/camera-preview` | `SceneCameraDraft` 검사; 저장 없음 |
| `POST /api/v1/scene_authoring/cameras` | `{draft, preview_sha256}` 재검사 후 불변 Assembly 저장 |

이미 열린 `PlatformClient`와 이 작업실의 실제 ID를 사용하는 예:

```python
draft = client.get(
    "scene_authoring/camera-template?scene_id=" + scene_id
    + "&assembly_id=" + source_assembly_id
)
# Explicitly select registered models and intended conditions in draft["assembly"].
plan = client.preview_scene_camera(draft)
if plan["status"] == "ready":
    saved = client.save_scene_camera(plan)
    assembly = saved["assembly"]
    render_draft = saved["render"]["request"]
```

`ready`는 카메라 실행 인터페이스의 연결 조건을 확인했다는 의미입니다. 실제 복사휘도·광학 근사 오차·실물 예측 정확도, 전체 불확실도, 표준 준수 또는 배포 승인을 의미하지 않습니다. API의 `physical_validation`은 `not_established`로 유지합니다. 등록된 모델이 없거나 광학 정보가 빠진 경우 먼저 근거가 있는 모델 버전을 등록해야 합니다. 실제 장비 측정은 0건이며 원문 일곱 모듈·네 실행 방식의 전체 Goal은 미완료입니다.

## v0.12에서 수정한 네 결함

| 발견한 문제 | 수정과 검증 범위 |
| --- | --- |
| Composer는 PASS지만 실제 파장·각도·Gain·RAW가 맞지 않는 구성을 `ready`로 표시 | 실제 Runtime 검사와 Readout 계약 검사를 사용하고 저장 전에 거부; 부모·선택 모델·Registry 불변 확인 |
| 모델 선택을 비우면 인덱스가 -1이 되어 다시 선택할 수 없음 | Lens·Sensor·ISP의 세 편집 슬롯을 보존하여 빈 값과 미등록 ID에서 복구 |
| 문법상 유효한 `{}`·`null`이나 잘못된 JSON으로 화면 재그리기 실패 | 잘못된 구조에서도 원문 편집기와 오류를 유지하고 검사·저장을 차단; 복구 뒤 새 검사 요구 |
| Worker가 정상·오류·충돌로 종료한 Runner의 자식 프로세스를 남김 | 모든 Runner 종료 경로에서 전용 프로세스 그룹 정리; 완료·취소 상태와 원본 근거 유지 |

## v0.12 검증 근거

| 범위 | 확인한 근거 |
| --- | --- |
| 전체 회귀 | [1,158개 통과](../reports/camera_platform/preparation-v012-tests.xml) |
| 별도 전환 검사 | [36개 통과](../reports/camera_platform/scene-preparation-transition-tests.xml); 전체 회귀의 6개와 중복 |
| 설치본 격리·재시작 | [14항목 통과](../reports/camera_platform/preparation-installed-runtime-verification.json); 개발 Checkout 변경·재시작 후 새 구성과 실제 출력 보존 |
| 별도 설치 QA 화면 | [장면 작성 36항목](../reports/camera_platform/preparation-ui/authoring-v012/verification.json), [카메라 준비 20항목](../reports/camera_platform/preparation-ui/installed-v012/verification.json) |
| 기본 작업실 | [7항목 통과](../reports/camera_platform/primary-preparation-verification.json); 원본 모델 보존·재시도·Mitsuba/RAW/RGB·배열 해시·근거 ZIP |

기본 작업실의 읽기 전용 화면 검사도 **6개 흐름·141항목**을 통과했습니다(POST 0회·브라우저 오류 0건). [흐름별 기본 UI 근거](camera-platform-installed-runtime.md#검증과-한계)는 별도 QA와 구분합니다. [최종 보존 검사](../reports/camera_platform/primary-preparation-integrity.json)와 [기존 시간 RAW/RGB 재실행](../reports/camera_platform/primary-preparation-legacy-rerun.json)도 통과했습니다. [기능 시험](../tests/platform/test_scene_camera.py)과 [브라우저 도구](../tools/verify_camera_scene_preparation_ui.cjs)가 검증 경로입니다. v0.11 인도 후의 개발 시험 **1,143개·브라우저 17항목**과 [초기 QA 예제](../reports/camera_platform/scene-camera-http-fixture.json)는 v0.12 인도 전 역사적 기록으로 보존합니다.

## Worker 종료 처리의 초기 개발 회귀 기록

전체 회귀 시험의 첫 시도는 1,130개 통과·1개 실패였습니다. SIGTERM을 무시하는 자식 프로세스를 SIGKILL로 종료한 뒤 재차 그룹 신호를 보내는 순간 `PermissionError`가 발생하여 종료 상태 기록이 중단되었습니다. 실패 기록은 `scene-camera-full-tests-first-attempt.xml`에 보존합니다.

Apple의 [XNU 신호 처리 소스](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_sig.c)의 `killpg1`은 zombie를 수신 대상에서 제외하며, 적격 수신자가 없을 때 EPERM을 반환하는 경로가 있습니다. 관측된 실패는 이 종료 경합과 일치하지만 실패 순간의 전체 그룹 스냅샷이 없어 그 당시 zombie 상태 자체를 확정하지는 않습니다.

수정된 Worker는 EPERM을 무조건 성공으로 처리하지 않습니다. Darwin에서만 새 프로세스 상태 조회가 성공하고 해당 그룹에 실행 중인 프로세스가 없음을 확인한 경우 종료 처리를 계속합니다. 실제 권한 거부, 실행 중인 구성원, 혼합 상태, 조회 시간 초과·파싱 실패는 계속 오류로 남깁니다. 호출자 자신의 그룹·브로드캐스트·잘못된 그룹 ID도 거부합니다. 실제 자식·손자 프로세스의 취소/시간 초과 종료 시험과 이 오류 경로의 별도 시험을 유지합니다.

v0.12 검토에서 추가로 정상·오류·강제 충돌 종료 때의 자식 잔존을 재현하여 위 공통 정리 경로로 보완했습니다. [Worker 시험](../tests/platform/test_worker_process_group.py)은 취소·시간 초과·종료 요청·정상 종료·오류·충돌·성공 결과를 포함해 SIGTERM을 무시하는 손자 프로세스 종료와 최종 기록을 확인합니다. 이 제한된 실행 시험은 지속 부하·일반 분산 Worker 운영 검증을 대신하지 않습니다.

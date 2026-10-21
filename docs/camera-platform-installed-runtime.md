# 검증된 설치 버전으로 실행하기

2026-09-23 기준 기본 설치본은 **v0.15.0**이며, [현재 인도 기록](../reports/camera_platform/delivery.json)을 따른다. **v0.16.0은 별도 설치 후보**로 검증 중이다. 독립 Python·인증된 자산 선택, 광학·전기·HDR·ISP 실행과 설치 전환의 상태는 [v0.16 후보 기록](camera-platform-v016-install-candidate.md)에 구분한다. 이 문서의 아래 버전별 검증은 도입 당시 기록이다.

**v0.14.0 도입 기록:** [처방 기반 OPD·위치별 PSF](camera-platform-prescription-optics.md)를 포함하며 [설치 전환](../reports/camera_platform/primary-prescription-upgrade.json)은 백업 후 기존 **215개 기록·74개 작업·437개 감사 이벤트**를 보존했다. [실제 기본 작업실 실행](../reports/camera_platform/prescription-primary-delivery.json), [기존 시간 RAW/RGB 재현](../reports/camera_platform/primary-prescription-legacy-rerun.json), [최종 무결성](../reports/camera_platform/primary-prescription-integrity.json)을 확인했다. 새 예제와 재실행은 8개 기록·5개 작업·23개 감사 이벤트를 추가했다.

기본 작업실의 [새 저장 결과 조회 12항목](../reports/camera_platform/prescription-ui/primary-v014/verification.json)은 Factory·Builder·Camera·ISP 재처리, 재열기와 모바일 배치를 확인하고 모든 DB 행을 유지했다. 이전 v0.13의 7개 흐름·168항목은 아래 역사적 검증이며 이번 버전의 추가 시험으로 합산하지 않는다. 실제 장비 측정은 **0건**이고 전체 상용화 Goal은 미완료다. Python 계산 의존성은 공유 환경이다. 버전·Wheel·Manifest와 한계는 [인도 기록](../reports/camera_platform/delivery.json)을 따른다.

## v0.14 설치·실행 근거

처방 OPD·위치별 PSF 추가분의 [설치본 격리·재시작 20항목](../reports/camera_platform/prescription-installed-runtime-verification.json)을 통과했다. 저장소 밖 실제 패키지 실행과 두 별도 Worker 상태에서 동일 입력의 RAW/RGB 바이트가 일치했다. 처방 재확인은 기존 작업 ID를 유지하며 개발 파일 변경 후와 재시작 후 각각 POST 0회로 원본·출력 해시를 확인했다. 기존 시간 촬영·제어·취득 분석·Mitsuba·카메라 준비·저장 광휘 재사용은 실제 재계산으로 재시작을 검증했다.

[후보 감사](../reports/camera_platform/prescription-v014-candidate-audit.json)는 회귀 1,337개, 전환 169개, ZIP 계약 22개 중 중복 78개를 제외한 **1,450개 시험**, 소스·정적 파일 133개, 설치 Manifest와 Wheel, [처방 UI 10항목](../reports/camera_platform/prescription-ui/installed-v014/verification.json)을 연결한다. 기존 Camera ZIP 검증기의 옛 해시 가정을 발견해 모델 계보·부모 Factory 원본까지 검증하도록 수정했으며 첫 실패 시도도 보존했다.

같은 설치본의 [장면 작성 36항목](../reports/camera_platform/prescription-ui/authoring-v014/verification.json), [카메라 준비 20항목](../reports/camera_platform/prescription-ui/preparation-v014/verification.json), [광량 재사용 26항목](../reports/camera_platform/prescription-ui/projection-v014/verification.json)을 완료한 뒤 전체 전환 Gate를 통과했다. [v0.13 원본 67개](../reports/camera_platform/release-archives/v0.13.0/manifest.json)와 [후보 입력 불변](../reports/camera_platform/prescription-v014-release-audit.json)을 확인했다. [검증 서버 정리](../reports/camera_platform/prescription-ui/qa-cleanup-v014.json)는 별도 QA·Worker·프록시 종료를 기록하며 기본 작업실 8770은 실행 중이다.

## v0.13 설치·실행 근거

[저장된 분광 복사휘도 재사용](camera-platform-scene-projection.md)의 개발 소스·Wheel·설치본·기본
작업실 실행을 구분하여 검증했다. [v0.13 패키지 보고서](../reports/camera_platform/release-archives/v0.13.0/package-verification.json)는
소스·정적 파일 **132개** 일치, Checkout 밖 `python -I` 실행과 Renderer를 다시 호출하지 않는
저장 광휘 → RAW 경로를 확인한다. 공유 계산 의존성을 사용한 범위다.

- [개발 회귀 1,216개](../reports/camera_platform/projection-development-tests.xml)의
  [소스·환경 결합 기록](../reports/camera_platform/projection-development-test-binding.json)은
  실행 전후 환경과 JUnit 해시를 보존한다.
- [별도 전환 시험 78개](../reports/camera_platform/projection-runtime-transition-tests.xml)의
  [결합 기록](../reports/camera_platform/projection-runtime-transition-tests-binding.json)은
  검사 도구·시험·직접 의존 파일 5개의 전후 SHA와 JUnit SHA를 보존한다. 기존 36개는 전체 회귀와
  중복되고 42개가 별도 추가분이다.
- [Node 상태 회귀 6항목](../reports/camera_platform/scene-projection-ui-state-regression.json)과
  [개발 QA 화면 26항목](../reports/camera_platform/projection-ui/development/verification.json)을
  설치본 재시작·기본 작업실 화면 증거와 구분한다.

새 Manifest에 `camera_platform/scene_projection.py`가 있으면 전환 도구의
`--projection-ui-proof`가 필수다. 패키지의 실제 재사용 실행, 설치본의 원본·새 변환·RAW/RGB
근거, 개발 파일 변경/재시작 뒤 같은 배열 해시, 같은 QA 상태·환경·주소의 화면 검사와
설치본 `scenes.js`/`style.css` 해시가 맞아야 한다. 기존 장면 작성·카메라 준비 Gate도 유지한다.
근거가 없거나 서로 다르면 백업·서버 종료·패키지 선택 전에 거부한다.

[설치본 격리·재시작](../reports/camera_platform/projection-installed-runtime-verification.json)
**17항목**은 개발 Checkout 변경·재시작 뒤 원본 `Lλ`·기존/새 `Eλ`·MCSE·RAW/RGB의 7개
해시가 같은지 확인했다. 검사 서버의 종료도 확인했다. 별도 설치 QA는
[광휘 재사용 26항목](../reports/camera_platform/projection-ui/installed-v013/verification.json),
[카메라 준비 20항목](../reports/camera_platform/projection-ui/preparation-v013/verification.json),
[장면 작성 36항목](../reports/camera_platform/projection-ui/authoring-v013/verification.json)을 통과했다.
기본 작업실의 백업·전환·원본 이력 보존과 실제 실행은 위 보고서로 확인했으며, 기본 화면의
읽기 전용 검사도 아래 168항목을 확인했다. 설치 QA와 기본 작업실 화면의 증거를 합쳐 표시하지 않는다.

[최종 인도 입력 감사](../reports/camera_platform/projection-release-input-audit.json) **14항목**은
현재 소스·정적 파일 132개와 Wheel·설치본의 일치, 설치 Manifest 688개 파일, Wheel RECORD,
전체 회귀의 제품 소스/환경 결합, 전환 시험의 직접 입력 5개 결합 및 v0.12 보관 파일 51개의
불변을 확인한다. 작성 화면은 전환 시 검증한 QA Runtime으로 정적 파일 출처를 연결하고,
재사용/준비 화면은 보고서의 정적 파일 해시도 대조한다. [최종 Runtime 상태](../reports/camera_platform/projection-v013-runtime-state.json)는
별도 QA 8776의 서버·Worker와 설치 검증 서버 종료, 기본 작업실의 정상 실행을 확인한다.
전체 회귀 결합은 제품 Python 코드·실행 환경의 결합이며, 수집 시점의 모든 시험 파일까지
해시로 고정한 기록은 아니다. 별도 전환 시험은 명시한 5개 직접 입력을 따로 고정했다.

## 기본 작업실의 화면 검증

v0.13 기본 작업실의 읽기 전용 검사 **7개 흐름·168항목**이다. 모든 흐름에서 POST와 브라우저
오류가 0건이며, 새 자산이나 실행을 만드는 별도 QA 검사와 구분한다.
[기존 6개 흐름의 요약](../reports/camera_platform/projection-ui/primary-readonly-summary.json)은
141항목과 검사 전후 기록 수 일치, POST·차단된 쓰기·브라우저 오류 0건을 함께 확인한다.

| 기본 작업실 화면 | 검증 결과 |
| --- | --- |
| 저장 광휘 재사용 | [27항목](../reports/camera_platform/projection-ui/primary-projection/verification.json) |
| 새 카메라 준비 | [23항목](../reports/camera_platform/projection-ui/primary-prepared/verification.json) |
| 3D 장면 | [17항목](../reports/camera_platform/projection-ui/primary-scenes/verification.json) |
| 응답 취득·분석 | [15항목](../reports/camera_platform/projection-ui/primary-response/verification.json) |
| 공통 작업실 | [29항목](../reports/camera_platform/projection-ui/primary-workbench/headless-qa.json) |
| AE/AWB 제어 | [26항목](../reports/camera_platform/projection-ui/primary-controls/verification.json) |
| 시간 촬영 | [31항목](../reports/camera_platform/projection-ui/primary-temporal/primary-readonly-qa.json) |

## 이전 인도 기록

**v0.12.0 인도 당시 기록이다.** [장면별 카메라 준비](camera-platform-scene-camera.md)와
준비 계약·화면 복구·Worker 종료 보완을 설치 패키지에 포함했다.
[설치 전환](../reports/camera_platform/primary-preparation-upgrade.json)은 기존 **208개 기록·68개 작업·412개 감사 이벤트**를 보존했다.
[기본 작업실 실행](../reports/camera_platform/primary-preparation-verification.json) **7항목**과
[설치본 격리·재시작](../reports/camera_platform/preparation-installed-runtime-verification.json) **14항목**을 확인했다.
버전·Wheel·Manifest 해시는 각 보고서와 [인도 기록](../reports/camera_platform/delivery.json)을 따른다.

회귀 시험 **1,158개**, 별도 전환 시험 **36개**를 통과했으며 6개는 두 범위에 중복된다.
별도 설치 QA의 장면 작성 **36항목**·카메라 준비 **20항목**과 기본 작업실의 읽기 전용 화면
**6개 흐름·141항목**도 통과했다(POST 0회·브라우저 오류 0건). 실제 장비 측정은 **0건**이며
물리 검증·총 불확실도와 원문 전체 상용화 Goal은 미완료다.

**이전 기록:** v0.11.0의 3D 작성/렌더링 인도와 이후 카메라 준비 개발 시험 1,143개·브라우저
17항목은 도입 당시 근거다. v0.12의 설치 검증은 위 보고서로 구분한다.

**v0.10.0 인도 당시 기록:** [RAW 응답 취득·공통 관측·분석 버전](camera-platform-response-ingest.md)을 기본 작업실에 설치했습니다.
원본 JSON 파일 바이트와 실제 RAW를 보존하며 같은 취득을 여러 지표로 분석하고 비교합니다.
플랫폼 회귀 시험 1,089개·전환 경합 2개, 설치본 실행/재시작 9항목과 설치본 브라우저 검사를 확인했습니다.
기존 기록 182개·작업 60개·감사 이벤트 362개와 시간 RAW/RGB를 보존했습니다.
독립 실측·총 불확실도와 전체 일곱 모듈·네 실행 방식의 상용화 Goal은 계속 진행 중입니다.

**v0.9.0 도입 기록.** [AE/AWB 제어](camera-platform-closed-loop.md)는
불변 정책·촬영 프로그램, 전체 노출/Gain 범위 검사, 공통 시험·API·SDK·별도 Worker,
32프레임 촬영과 RAW 재처리를 작업실에서 연결합니다. 요청·양자화·실제 적용과 목표 잔차를
보존하며, 현재 폐루프 관측을 기존 비교기로 물리 검증 PASS 처리하지 않습니다.
[패키지 검증](../reports/camera_platform/package-verification.json),
[설치본 격리·재시작 검증](../reports/camera_platform/controls-installed-runtime-verification.json),
[기존 기록 보존 전환](../reports/camera_platform/primary-controls-upgrade.json)을 확인했습니다.
전체 상용화 Goal은 진행 중이며 독립 실측, 일반 3D/물리 모델, HDR/LFM, 전체 불확실도,
고급 최적화·Agent·권한·고객 환경 배포는 남아 있습니다.

아래 버전별 설명은 각 기능의 도입 시점과 당시 검증 범위를 보존한 기록입니다.
최신 인도 근거는 [delivery.json](../reports/camera_platform/delivery.json)의 버전과 해시를 따릅니다.

**v0.8.1 도입 기록.** [작업실 조회 경량화](camera-platform-workbench.md)는
작업·모델·예측 이력의 명시적 요약, 선택 원본 조회, 작은 SDK Session과 검증된 일괄 DB
읽기를 추가합니다. 원본·계산·물리 검증 범위는 보존하며 전체 상용화 Goal은 진행 중입니다.
아래 v0.8.0 이하 설명은 해당 기능이 추가된 버전의 기록입니다.

기본 작업실은 개발 Checkout과 별도로 설치된 검증된 패키지를 선택해 실행합니다.
Python 코드, 웹 화면, 패키지 데이터의 파일 목록과 SHA-256을 고정합니다. 개발 소스를
수정해도 API와 Worker는 선택한 패키지를 계속 사용합니다. 기존 자산·시험·Run·Blob은
같은 Registry에 남습니다.

## 준비와 실행

실제 설치 실행을 확인한 보고서와 그 보고서의 정확한 Wheel이 필요합니다.

```sh
.venv-platform/bin/python tools/install_camera_platform_runtime.py \
  --wheel reports/camera_platform/packages/pyisetcam-0.1.0-py3-none-any.whl \
  --verification-report reports/camera_platform/package-verification.json \
  --activate
```

`--activate`를 생략하면 설치만 준비합니다. 설치 경로는 Wheel과 검증 환경의 해시로
구분하며, 파일과 디렉터리는 읽기 전용 권한으로 만듭니다. 저장 위치는
`outputs/camera-platform-runtimes/`입니다. 작업실의 `installed-runtime.json`에는
정확한 설치 경로와 Manifest 해시가 있습니다. 선택 변경은 실행 중인 서버를 중단하거나
새 코드를 자동 적용하지 않습니다.

기존 서버와 선택한 패키지가 다르면 런처는 이를 알리고 기존 서버를 유지합니다.
작업 종료와 백업을 확인하고 해당 서버를 정상 종료한 다음 실행합니다.

```sh
.venv-platform/bin/python tools/start_camera_platform.py
```

[Start Camera Platform.command](../simulations/camera_platform/Start%20Camera%20Platform.command)도
같은 선택을 사용합니다. PID, 실제 import 경로, 환경, API 버전, 제공하는 HTML/JS/CSS가
선택한 패키지와 일치해야 합니다. Meep/Mitsuba 설정이 있으면 설치 패키지의 고정 Adapter로
Interpreter와 실제 서버의 Binding도 확인합니다. 개발은 별도 작업실로 실행합니다.

```sh
.venv-platform/bin/python tools/start_camera_platform.py --development \
  --state-dir outputs/camera-platform-dev --port 8771
```

## 이전 패키지 선택과 복구

이미 준비된 디렉터리는 `--select /absolute/path/to/previous-prepared-runtime`으로
선택합니다. 변경 전 선택은 작업실의 `runtime-pin-history/`에 보존됩니다. 선택 후에는
동일한 정상 종료·재기동 절차가 필요합니다. **패키지 선택은 DB Schema의 이전 버전
호환성을 보증하지 않습니다.** 과거 v0.12→v0.13 전환은 동일 Registry Schema에서 기존
기록을 보존한 업그레이드입니다. 임의 Schema Migration/Downgrade의 보장은 아닙니다.

Registry 백업 ZIP에는 설치 패키지, Pin, Meep/Mitsuba Interpreter와 `runtime-config.json`이
포함되지 않습니다. 복구 시 검증된 Wheel·보고서·Interpreter를 별도로 준비하고 새 상태
디렉터리에서 패키지와 Solver 설정을 다시 선택합니다. 다른 기기의 경로를 담은 Pin이나
이전 PID 파일을 복사해 실행하지 않습니다.

## 검증과 한계

- [v0.12 회귀 시험](../reports/camera_platform/preparation-v012-tests.xml): **1,158개**.
- [v0.12 전환 시험](../reports/camera_platform/scene-preparation-transition-tests.xml): **36개**;
  전체 회귀에 포함된 6개와 별도 사례 30개다.
- [v0.12 설치 실행·재시작](../reports/camera_platform/preparation-installed-runtime-verification.json):
  **14항목**. 새 구성의 저장·재시도와 실제 렌더링/RAW/RGB, 손상된 개발 Checkout과 재시작을
  거친 환경·원본 해시·감사 기록 보존을 확인한다.
- 별도 설치 QA의 [장면 작성](../reports/camera_platform/preparation-ui/authoring-v012/verification.json)
  **36항목**, [카메라 준비](../reports/camera_platform/preparation-ui/installed-v012/verification.json)
  **20항목**. 기본 작업실 UI 검증과 별도 증거다.

기본 작업실의 읽기 전용 화면 검사는 아래 **141항목**이며, 모든 흐름에서 POST와 브라우저
오류가 0건이었다. 새 기록을 만드는 QA 화면 검사와 구분한다.

| 기본 작업실 화면 | 검증 결과 |
| --- | --- |
| 새 카메라 준비 | [23항목](../reports/camera_platform/preparation-ui/primary-prepared/verification.json) |
| 3D 장면 | [17항목](../reports/camera_platform/preparation-ui/primary-scenes/verification.json) |
| 응답 취득·분석 | [15항목](../reports/camera_platform/preparation-ui/primary-response/verification.json) |
| 공통 작업실 | [29항목](../reports/camera_platform/preparation-ui/primary-workbench/headless-qa.json) |
| AE/AWB 제어 | [26항목](../reports/camera_platform/preparation-ui/primary-controls/verification.json) |
| 시간 촬영 | [31항목](../reports/camera_platform/preparation-ui/primary-temporal/primary-readonly-qa.json) |

[최종 무결성 검사](../reports/camera_platform/primary-preparation-integrity.json)는 새 예제와
재실행 이후에도 원래 208개 기록·68개 작업·412개 감사 이벤트를 보존했음을 확인한다.
[기존 시간 촬영 재실행](../reports/camera_platform/primary-preparation-legacy-rerun.json)은
실제로 내려받은 RAW/RGB 바이트가 앞선 설치 버전의 결과와 같음을 확인한다.

아래는 설치 Runtime이 처음 도입될 때의 역사적 검증 기록이다.

- [런처 시험](../reports/camera_platform/installed-runtime-tests.xml): 파일 변경·누락·추가,
  링크·특수 파일, 환경·Manifest 변경, Python 최적화 옵션에 따른 검사 우회 방지와
  기존 Meep/Finder 런처 동작.
- [독립 실행 검사](../reports/camera_platform/installed-runtime-verification.json): 가져오면
  실패하도록 만든 가짜 개발 Checkout에서 실제 Prysm→시간 RAW/RGB→ISP Replay 실행,
  개발 파일 변경 후 동일 RAW, 기존 서버 재사용, 재시작 후 동일 RAW·감사 이력 확인.
- [기본 작업실 전환 검사](../reports/camera_platform/primary-installed-runtime-verification.json):
  백업, 전후 기록 수, 실행 경로, 실제 재계산 환경과 RAW/RGB 해시.

이 방식은 **코드·화면·패키지 데이터의 분리**입니다. 현재 `.venv-platform`과 기존
계산 의존성을 공유합니다. 검사하는 버전은 `environment.packages`에 기록된 계산
패키지와 Python/OS이며 전체 전이 의존성 잠금·바이너리 해시 검증을 대신하지 않습니다.
실행 중 공유 환경을 업그레이드하지 않고 새 기능은 새 Wheel 설치 검증 후 배포합니다.

읽기 전용 권한과 해시는 우발적 변경 탐지용이며 서명·승인 또는 로컬 관리자에 대한
보호가 아닙니다. 깨끗한 지원 OS 설치, 의존성 잠금, 프로젝트 권한, 서명된 배포,
일반적인 Schema Migration/복귀는 상용화 Gate에 남아 있습니다.

v0.5→v0.6은 선택 필드가 추가된 동일 Registry Schema입니다.
[기본 작업실 업그레이드](../reports/camera_platform/primary-uncertainty-upgrade.json)는
전환 전 백업, 기존 기록 보존, 설치 경로·Worker 환경과 같은 시간 RAW/RGB를 확인했습니다.
[입력 경계 실행](../reports/camera_platform/primary-uncertainty-verification.json)과
[기본 화면](../reports/camera_platform/uncertainty-ui/primary/headless-qa.json)도 확인했습니다.
이 증거를 임의 버전의 Migration이나 Downgrade 보장으로 확대하지 않습니다.

v0.6→v0.7 당시 전환도 [기록 보존·실제 시간 RAW/RGB](../reports/camera_platform/primary-studies-upgrade.json)로 확인했습니다.
설계 탐색은 별도 `studies.js` 모듈로 제공되며 런처는 Manifest에 포함된 모든 정적 파일을
실제 HTTP Byte와 대조합니다. 이전 패키지의 정적 파일 목록도 그대로 검사합니다.
새 Study 기록은 Schema 1의 불변 Record로 저장하며 임의 Downgrade는 검증하지 않았습니다.

v0.7→v0.8 당시 전환은 [전환 근거](../reports/camera_platform/primary-surrogate-upgrade.json)에
백업과 기존 Record/Job 보존, 실제 시간 RAW/RGB 재계산, 모든 정적 파일 HTTP 대조를 남깁니다.
[설치 패키지 검사](../reports/camera_platform/package-verification.json)는 109개 소스/정적 파일과
기존 Solver·공간/시간 계산·RAW Replay·입력 경계·설계 실험·Surrogate를 실행합니다.
Surrogate 학습은 기존 Job 저장 구조, 예측은 새 불변 Record 종류를 사용합니다.
구버전에서 새 기록을 소비하는 Downgrade와 완전한 의존성 격리는 검증 범위가 아닙니다.

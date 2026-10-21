# 독립 의존성 환경 준비와 검증

기존 설치 Runtime은 코드·정적 파일·데이터를 고정하지만 `.venv-platform`의 계산 라이브러리를
공유합니다. 고객 설치를 준비하려면 별도 가상 환경에서 패키지 의존성 해결과 실제 계산을
확인해야 합니다. 아래 기록은 v0.14 당시 별도 독립 환경 검증입니다. 2026-09-23의 기본
작업실은 v0.15이며, [v0.16 설치 후보](camera-platform-v016-install-candidate.md)는 새 환경과
명시적 Python·자산 결합으로 추가 검증 중입니다.

## 발견한 설치 문제

2026-09-20의 원래 환경에서 `pip check`가 실패했습니다. RayOptics의 IPython, ipywidgets,
PySide6, qtconsole 의존성이 없고, OpticalGlass 1.2.0은 NumPy 2.4.3 이상을 선언하지만
기존 계산 환경은 NumPy 2.4.2였습니다. 기존 계산 성공은 이 설치 불일치를 해소하지 않습니다.
[원본 검사 결과](../reports/camera_platform/independent-environment/original-dependency-check.json)를
보존했으며 기본 작업실의 공유 의존성 환경은 수정하지 않았습니다.

## 새 환경 준비

검증된 Wheel과 그 Wheel의 실행 보고서를 입력으로 사용합니다. 기존 보고서는 Wheel의
식별 근거이며, 새 의존성 환경에서의 계산 성공 근거로 승계하지 않습니다.

```sh
.venv-platform/bin/python tools/prepare_camera_platform_environment.py \
  --wheel reports/camera_platform/packages/pyisetcam-0.1.0-py3-none-any.whl \
  --verification-report reports/camera_platform/package-verification.json \
  --destination outputs/camera-platform-independent-env/customer-candidate
```

부모 디렉터리는 존재해야 하고 대상 디렉터리는 없어야 합니다. 기존 가상 환경이나 작업실을
덮어쓰지 않습니다. `platform`, `factory-rayoptics`, `factory-prysm` 의존성을 정상 resolver로
설치하고 `pip check`, 격리 import, 패키지 위치, 소스 지문을 확인합니다. 준비 결과는 대상
디렉터리의 `environment-report.json`이며 `prepared`는 의존성 준비 완료만 뜻합니다.

`pip-installation-report.json`에는 받은 파일의 URL·SHA256·패키지 메타데이터를 보존합니다.
[pip 설치 보고서](https://pip.pypa.io/en/stable/reference/installation-report/) 자체는 잠금파일이
아니므로 별도의 `dependency-lock.json`과 `requirements.lock.txt`를 생성합니다.
재생 시 [필수 해시 검사](https://pip.pypa.io/en/stable/topics/secure-installs/)를 적용합니다.

```sh
.venv-platform/bin/python tools/prepare_camera_platform_environment.py \
  --wheel reports/camera_platform/packages/pyisetcam-0.1.0-py3-none-any.whl \
  --verification-report reports/camera_platform/package-verification.json \
  --destination outputs/camera-platform-independent-env/customer-replay \
  --lock outputs/camera-platform-independent-env/customer-candidate/dependency-lock.json
```

잠금파일은 다운로드한 입력을 고정합니다. Prysm은 소스 배포본이므로 빌드 의존성·생성 Wheel
바이트까지 재현한다고 주장하지 않습니다. 기본 Python, 가상 환경의 pip, OS 라이브러리와
외부 Meep/Mitsuba 환경도 별도 관리 대상입니다. 같은 Python·OS·아키텍처와 기록된 배포 파일에
대한 접근이 필요합니다. 현재 도구는 오프라인 설치 패키지나 독립 Python 배포판이 아닙니다.

## 실제 계산 검증

아래 도구는 설치된 제품 파일 전체를 Wheel과 바이트 단위로 대조하고 import 위치와
`pip check`를 검사합니다. 이후 기존 통합 계산 시나리오를 변경 없이 Checkout 밖에서
새 Python으로 실행하여 별도 결과를 남깁니다. 이전 근거 파일은 덮어쓰지 않습니다.

```sh
.venv-platform/bin/python tools/verify_camera_environment_execution.py \
  --python outputs/camera-platform-independent-env/customer-candidate/bin/python \
  --wheel reports/camera_platform/packages/pyisetcam-0.1.0-py3-none-any.whl \
  --package-proof reports/camera_platform/package-verification.json \
  --meep-python /absolute/path/to/meep/bin/python \
  --mitsuba-python /absolute/path/to/mitsuba/bin/python \
  --output reports/camera_platform/independent-environment/customer-execution.json
```

검증 범위는 Factory, RAW/RGB, 재처리, 시간 촬영, AE/AWB, 설계 탐색·Surrogate,
응답 분석, 장면 준비·렌더링·저장 광휘 재사용, 처방 광학과 부모 근거 ZIP입니다.
API TestClient에서 실행하며 화면 검증·별도 Worker 프로세스·고객 OS 설치 검증과 구분합니다.
기존 기본 RAW, 장면 광휘, 처방 RAW/RGB/재처리 RGB의 다섯 해시 비교도 따로 기록합니다.

## 확인한 범위와 남은 조건

2026-09-20에 새 CLI 설치와 잠금 재생, 새 환경의 실제 통합 계산을 각각 확인했습니다.
[통합 근거 색인](../reports/camera_platform/independent-environment/evidence-summary.json)은
각 보고서·잠금파일·실패 기록의 경로와 SHA256, 직접 비교한 92개 artifact identity를 보존합니다.

| 단계 | 실제 결과 | 근거 |
| --- | --- | --- |
| `20260920-cli-r2` 새 환경 준비 | 92개 artifact 설치, `pip check` 통과, 격리 import·설치 경로 확인 | [환경 보고서](../outputs/camera-platform-independent-env/20260920-cli-r2/environment-report.json) |
| `20260920-cli-replay` 잠금 재생 | 해시 강제 설치와 `pip check` 통과, 92개 이름·버전·SHA256 모두 r2와 동일 | [재생 보고서](../outputs/camera-platform-independent-env/20260920-cli-replay/environment-report.json) |
| r2의 설치 패키지 통합 계산 | 681개 제품 파일이 Wheel과 동일, 계산 통과, 기존 다섯 출력 해시와 동일, 검증 프로세스 종료 확인 | [계산 보고서](../reports/camera_platform/independent-environment/cli-execution.json) |
| 잠금 재생 환경의 통합 계산 | 별도 실제 계산 통과, 681개 제품 파일 및 기존 다섯 출력 해시 동일, 검증 프로세스 종료 확인 | [재생 계산](../reports/camera_platform/independent-environment/replay-execution.json) · [두 환경 비교](../reports/camera_platform/independent-environment/replay-calculation-comparison.json) |
| r2의 별도 API·Worker와 재시작 | 실제 처방 RAW/RGB·ISP 재처리, 새 PID로 재시작 후 18개 기록·4개 작업·30개 감사 이력과 결과 해시 동일 | [실행·재시작](../reports/camera_platform/independent-environment/detached-qa-v014/verification.json) |
| r2의 재시작 후 화면 조회 | Builder → 저장 Camera → 저장 Replay, 새로고침 후 재조회; 8개 확인 항목, 브라우저 오류·경고 0, 모든 DB 행 동일 | [CUA 화면 검증](../reports/camera_platform/independent-environment/detached-qa-v014/ui/verification.json) |
| 준비·계산 검증·UI 오류 주입 도구 | 86개 시험 통과, 실패·건너뜀 0, Ruff 통과, 실행 전후 도구·시험 해시 동일 | [시험 보고서](../reports/camera_platform/independent-environment/verification-tests.json) · [JUnit](../reports/camera_platform/independent-environment/verification-tests.xml) |

새 환경은 NumPy 2.5.3, SciPy 1.18.1, Pillow 12.3.0을 사용하며 기존 후보 환경과 다릅니다.
계산 증거는 r2와 잠금 재생 환경에서 각각 직접 얻었습니다. 두 환경의 제품 파일과
계산 환경 지문, 기본 RAW·장면 광휘·처방 RAW/RGB·재처리 RGB의 다섯 해시가 동일합니다.
각 준비 보고서는 당시의 기록이므로 `calculation_validation`을 소급 변경하지 않습니다.
그 뒤 실행한 계산 성공은 위 별도 보고서에 기록합니다.

첫 CLI 시도인 `20260920-cli`는 설치와 `pip check` 뒤 metadata 검사에서 실패했습니다.
setuptools가 추가한 `_vendor` 경로의 packaging/platformdirs metadata를 직접 설치된
동일 이름 패키지와 중복으로 판단한 것이 원인이었습니다. 설치 목록은 가상 환경의
`purelib/platlib`에서 수집하고, import로 노출된 전체 목록은 별도로 보존하도록 수정했습니다.
두 목록 모두 가상 환경 내부 경계를 확인하며 직접 설치 목록의 실제 중복은 계속 거부합니다.
외부 패키지 설치 없이 만든 별도 가상 환경의 실제 `-I` probe로 이 사례를 회귀 검증했습니다.
[실패 보고서](../outputs/camera-platform-independent-env/20260920-cli/environment-report.json)와
[당시 probe 로그](../outputs/camera-platform-independent-env/20260920-cli/isolated-probe.log)는
수정하지 않았습니다.

앞선 [탐색 설치](../reports/camera_platform/independent-environment/exploratory-result.json)와
[탐색 계산](../reports/camera_platform/independent-environment/exploratory-calculation.json)은
별도 실험으로 유지합니다. 도구의 86개 시험에는 합성 패키지·mock resolver 계약 시험과
실제 격리 Python·프로세스 정리·오류 주입 proxy 경계 검사가 포함됩니다. 이 시험 개수는
화면 기능이나 물리 정확도 검증 개수가 아닙니다.

## 독립 환경의 별도 실행

기존 `start_camera_platform.py`와 설치 CLI는 개발용 `.venv-platform`을 우선 선택합니다.
새 환경의 실행 근거로 그 CLI의 성공을 그대로 사용할 수 없습니다.
`verify_camera_independent_worker.py`는 새 환경의 실제 계산 증거와 현재 격리 import를 대조한 뒤
그 Python을 명시하여 불변 Runtime, 별도 API와 Worker를 실행합니다. 개발 코드가 잘못된
내용인 외부 작업 폴더에서 실행하며 기본 작업실에는 접근하지 않습니다.

```sh
.venv-platform/bin/python tools/verify_camera_independent_worker.py \
  --python outputs/camera-platform-independent-env/customer-candidate/bin/python \
  --environment-report outputs/camera-platform-independent-env/customer-candidate/environment-report.json \
  --execution-proof reports/camera_platform/independent-environment/customer-execution.json \
  --wheel reports/camera_platform/packages/pyisetcam-0.1.0-py3-none-any.whl \
  --output-directory reports/camera_platform/independent-environment/customer-worker-qa
```

실제 처방 특성 생성·모델 등록·Camera·Replay는 11회 POST로 수행하고, 재시작 뒤에는 저장된
기록을 POST 없이 다시 검증합니다. 기본 RAW/RGB와 재처리 RGB는 앞선 독립 환경 계산과
바이트가 같습니다. 최초 시작·재시작 준비 중 실패한 프로세스의 정리 경로도 검사합니다.
성공하면 후속 화면 확인을 위해 시험 서버를 남기며 보고서에 PID·포트·상태 경로와 정리
담당을 명시합니다. 이번 실제 화면 확인 후 [정리 기록](../reports/camera_platform/independent-environment/detached-qa-v014/cleanup.json)은 API·Worker 종료, 포트 닫힘과 모든 행의 보존을 확인합니다. [후속 근거 색인](../reports/camera_platform/independent-environment/runtime-evidence-summary.json)은 실행·화면·17개 도구 시험·독립 검토와 기본 작업실 보존을 함께 묶습니다.

당시 기본 작업실은 v0.14이며 원래 공유 의존성 환경을 유지했습니다. 독립 환경의 별도 Worker·
재시작과 위 화면 조회는 통과했지만, 새 환경을 기본 실행기로 선택·전환하는 작업과 모든
화면의 전체 회귀 검증은 별도 출시 조건입니다. 잠금 재생 환경은 통합 계산까지 확인했으며
별도 API·Worker와 화면 검증은 r2에서 수행했습니다.
외부 Solver 독립 설치, 고객 OS·부하·운영 검증, 배포권 확인, 독립 실측과 총 불확실도는
계속 남아 있습니다.

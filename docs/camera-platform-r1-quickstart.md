# CameraE2E R1 설치·사용·복구 안내

**상태: 0.16.1rc1 엔지니어링 RC 검증 완료, 제품 승인 전.** [R1 지원 범위·선택형 기능·제한](camera-platform-r1-support.md)을 먼저 확인하세요. 정식 지원은 등록된 16×16·500/600 nm 정적 설계 실험 경로다. [설치본 검증](../reports/camera_platform/r1-rc-20260927/installed-proof-rc3/install-evidence.json)은 12개 gate를, [RC manifest](../reports/camera_platform/r1-rc-20260927/product-release-manifest-rc3.json)는 파일·지원 문서·검증 증거의 일치를 기록한다. `rc_ready`는 상용 승인이나 실물 예측 정확도 보증이 아니다.

## 1. RC 파일을 고정 의존성과 함께 설치

검증한 호스트는 **macOS arm64 / Python 3.12.13**입니다. 아래 RC kit에는 SHA-256이 고정된 wheel과 41개 의존성 wheel, 오프라인 lock이 있습니다. 새 venv와 프로젝트 데이터는 개발 checkout 및 기본 v0.15 데이터 폴더 밖에 둡니다. 다른 OS·Python 조합은 별도 wheel과 검증이 필요합니다.

아래 오프라인 설치는 별도 새 환경에서 실행해 `pip check`와 설치 파일 전체 해시를 확인했습니다. Factory RayOptics/Prysm 및 외부 MEEP/Mitsuba solver는 R1 정식 설계 실험 경로의 필수 의존성이 아닙니다.

```sh
R1_KIT="/Users/seongcheoljeong/Documents/CameraE2E/reports/camera_platform/r1-rc-20260927"
R1_BASE_PYTHON="<Python 3.12 executable absolute path>"
R1_VENV="<new R1 virtualenv absolute path outside the checkout>"
R1_WHEEL="$R1_KIT/packages/pyisetcam-0.16.1rc1-py3-none-any.whl"
R1_LOCK="$R1_KIT/requirements-r1-offline.lock.txt"
R1_WORKSPACE="<new R1 workspace absolute path outside the source checkout>"
R1_STATE="<new R1 state directory absolute path, separate from v0.15>"
R1_PROJECT_ID="camera-r1-pilot"

shasum -a 256 "$R1_WHEEL"  # bf86c70c28723cb5475275e82273f9842f44d6d7853263a18b9bb38e0fc1d7f5
"$R1_BASE_PYTHON" -I -B -m venv "$R1_VENV"
"$R1_VENV/bin/python" -I -B -m pip --isolated install --no-cache-dir --no-index \
  --find-links "$R1_KIT/packages" --find-links "$R1_KIT/wheelhouse" \
  --require-hashes -r "$R1_LOCK"
"$R1_VENV/bin/python" -I -B -m pip check
"$R1_VENV/bin/python" -I -B -c "import camera_platform; print(camera_platform.__version__)"
```

lock은 정확히 위 `R1_WHEEL` SHA-256의 `pyisetcam[platform]`과 41개 의존성의 버전·SHA-256을 고정합니다. 설치한 패키지의 예제 모델·장면·부품은 `camera_platform/data/r1`에 포함됩니다. 새 실험에는 과거 QA 폴더가 필요하지 않습니다. 외부 자산 번들을 쓰는 경우에만 `CAMERAE2E_R1_ASSET_BUNDLE`에 그 번들의 절대 디렉터리를 지정합니다. 번들의 모델·장면·부품·manifest 해시가 실행 기록에 남습니다.

## 2. 별도 R1 프로젝트를 열고 작업실 실행

다음 명령은 새 state에 `project-mode.json`을 만들고 `project_id`를 고정합니다. 기본 v0.15 앱의 state를 가리키지 마세요. `init-r1`은 데이터베이스를 초기화하거나 실험을 실행하지 않습니다.

```sh
mkdir -p "$R1_WORKSPACE"
"$R1_VENV/bin/python" -I -B -m camera_platform init-r1 \
  --workspace "$R1_WORKSPACE" --state-dir "$R1_STATE" \
  --r1-project-id "$R1_PROJECT_ID"

"$R1_VENV/bin/python" -I -B -m camera_platform serve \
  --workspace "$R1_WORKSPACE" --state-dir "$R1_STATE" \
  --r1-project-id "$R1_PROJECT_ID" --port 8798 --empty
```

서버는 로컬 `127.0.0.1:8798`에서 동작하고 내장 Worker가 제출된 작업을 처리합니다. 브라우저에서 `http://127.0.0.1:8798/`의 통합 작업실을 열고, 같은 출처의 `http://127.0.0.1:8798/design-experiments.html`로 이동합니다. 다른 포트의 구 개발 서버를 열 필요가 없습니다. 작업실의 인증 세션은 이 로컬 출처 안에서 사용합니다.

## 3. 모델·장면을 선택해 RAW와 RGB, 후보 결정을 저장

설계 실험 화면의 등록 카탈로그에서 모델 1–3개와 장면 1개(`r1-flat-v1` 또는 `r1-checker-v1`)를 고릅니다. 기존 광학 비교는 `r1-stop-clear-v1`/`r1-stop-quarter-v1`, 픽셀·전기 변형 비교는 `r1-clear-lowgreen-v1`/`r1-clear-8ke-v1`/`r1-clear-lowgreen-8ke-v1`을 선택합니다. 이들은 **등록된 조합**이며 임의 부품 자유 조합은 아닙니다. 두 장면은 각 장면으로 별도 실험을 제출합니다. 노출·gain·훈련 광량·별도 평가 광량·신호/SNR/포화/노출 제한과 seed를 입력하고 **시뮬레이션 한계 확인**을 선택한 뒤 제출합니다. 화면의 후보·취득 수 상한을 넘기면 범위를 줄여야 합니다.

실행은 선택 모델의 파장별 축상 광량을 장면 복사휘도에 한 번 적용하고, Lens PSF·위치별 조도·입사각, 픽셀 전하, 독립 reset 전기 readout, **정수 RAW → 고정 Software ISP → RGB** 순서로 계산합니다. 저장된 후보 표에서 신호·SNR·포화 제약과 선택 이유를 비교합니다. 적합 후보가 없으면 `선택 없음`을 그대로 보존하며 평가 단계에서 재선택하지 않습니다. 이 결과는 물리 승인이나 실물 정확도 판정이 아닙니다.

저장 목록에서 실험을 다시 선택해 결과·원본 정수 RAW·원본 float RGB·RGB PNG 미리보기·진단 배열을 엽니다. `전체 기록 JSON`, `metrics.csv`, 원본 RAW/RGB 링크를 내려받아 보관합니다. 목록 조회나 재열기는 노출을 다시 취득하지 않습니다. 동일 입력·seed의 재현성과 설치본 최대 123회 취득의 시간·메모리·저장량은 [검증 기록](../reports/camera_platform/r1-rc-20260927/installed-proof-rc3/install-evidence.json)에 있습니다.

## 4. 프로젝트 데이터 백업·복원

다른 데이터와 섞이지 않도록 R1 state 전체를 별도 보관합니다. 백업과 복원 모두 **동일한 프로젝트 ID**를 요구합니다. 복원 대상은 기존 state가 아닌 새 디렉터리여야 하고, 복원 전에 `init-r1`을 실행하지 않습니다.

```sh
R1_ARCHIVE="<new R1 backup zip absolute path>"
R1_RESTORED_STATE="<new empty restore destination absolute path>"

"$R1_VENV/bin/python" -I -B -m camera_platform backup \
  --workspace "$R1_WORKSPACE" --state-dir "$R1_STATE" \
  --output "$R1_ARCHIVE" --expected-project-id "$R1_PROJECT_ID"

"$R1_VENV/bin/python" -I -B -m camera_platform restore \
  --archive "$R1_ARCHIVE" --state-dir "$R1_RESTORED_STATE" \
  --expected-project-id "$R1_PROJECT_ID"

"$R1_VENV/bin/python" -I -B -m camera_platform serve \
  --workspace "$R1_WORKSPACE" --state-dir "$R1_RESTORED_STATE" \
  --r1-project-id "$R1_PROJECT_ID" --port 8799 --empty
```

복원 서버는 `127.0.0.1:8799`에서 열어 저장 목록·선택 결과·원본 artifact를 확인합니다. 원래 서버가 같은 데이터를 쓰는 동안 복원본과 경로를 혼동하지 마세요. 기본 v0.15 앱 교체나 데이터 이전은 이 절차에 포함되지 않습니다. 배포 파일과 환경도 복구 대상에 함께 보관해야 동일 코드를 다시 실행할 수 있습니다.

예제 광학·장면은 내부 기록상 MIT로 선언된 작성 합성 fixture에서 왔고, 권리 출처와 고지 조건은 [예제 번들 manifest](../examples/r1/manifest.json)에 고정했습니다. 배포에는 CameraE2E `LICENSE`를 동봉합니다. 실측·vendor HW ISP 등가성·자유 결합 HDR/움직임/LFM은 [지원표](camera-platform-r1-support.md)의 후속 또는 실험 범위입니다.

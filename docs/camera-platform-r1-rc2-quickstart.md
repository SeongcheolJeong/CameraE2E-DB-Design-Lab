# CameraE2E R1 0.16.1rc2 설치·사용·복구

[RC2 지원 범위](camera-platform-r1-rc2-support.md)를 먼저 확인한다. 이 설치본은
macOS arm64·Python 3.12 환경을 대상으로 만든 엔지니어링 후보이며, 제품 승인이나
실물 카메라 예측 정확도 보증이 아니다. 기본 v0.15 상태와 분리된 새 디렉터리에
설치한다.

## 1. 오프라인 설치

배포 ZIP을 해제한 뒤 아래 경로를 자신의 환경에 맞게 지정한다. 압축 최상위에는
`docs/`, `examples/`, `LICENSE`가 있고, 그 아래 `R1_KIT`에는 `packages/`,
`wheelhouse/`, `requirements-r1-offline.lock.txt`가 있다. wheel과 lock의 SHA-256은
[RC2 manifest](../reports/camera_platform/r1-rc2-20260927/product-release-manifest.json)에
기록된 파일과 대조한다. 새 가상 환경에서는 개발 checkout, 과거 QA 폴더, 외부
Prysm/solver가 필요하지 않다.

```sh
R1_ROOT="<extracted CameraE2E-R1-0.16.1rc2 directory>"
R1_KIT="$R1_ROOT/reports/camera_platform/r1-rc2-20260927"
R1_BASE_PYTHON="<Python 3.12 executable absolute path>"
R1_VENV="<new virtualenv absolute path outside the checkout>"
R1_WORKSPACE="<new workspace absolute path outside the checkout>"
R1_STATE="<new R1 state directory, separate from v0.15>"
R1_PROJECT_ID="camera-r1-pilot"

"$R1_BASE_PYTHON" -I -B -m venv "$R1_VENV"
"$R1_VENV/bin/python" -I -B -m pip --isolated install --no-cache-dir --no-index \
  --find-links "$R1_KIT/packages" --find-links "$R1_KIT/wheelhouse" \
  --require-hashes -r "$R1_KIT/requirements-r1-offline.lock.txt"
"$R1_VENV/bin/python" -I -B -m pip check
"$R1_VENV/bin/python" -I -B -c "import camera_platform; print(camera_platform.__version__)"
```

마지막 명령은 `0.16.1rc2`를 출력해야 한다. 기존 state나 패키지에 덮어 설치하지
않는다. [설치 검증 기록](../reports/camera_platform/r1-rc2-20260927/installed-proof-release/install-evidence.json)은
새 venv의 파일 전체 해시·실제 Worker/브라우저 흐름·복구를 기록한다.

## 2. R1 프로젝트와 작업실

```sh
mkdir -p "$R1_WORKSPACE"
"$R1_VENV/bin/python" -I -B -m camera_platform init-r1 \
  --workspace "$R1_WORKSPACE" --state-dir "$R1_STATE" \
  --r1-project-id "$R1_PROJECT_ID"
"$R1_VENV/bin/python" -I -B -m camera_platform seed-r1-modes \
  --workspace "$R1_WORKSPACE" --state-dir "$R1_STATE" \
  --r1-project-id "$R1_PROJECT_ID"
"$R1_VENV/bin/python" -I -B -m camera_platform serve \
  --workspace "$R1_WORKSPACE" --state-dir "$R1_STATE" \
  --r1-project-id "$R1_PROJECT_ID" --port 8798 --empty
```

브라우저에서 `http://127.0.0.1:8798/`를 열면 한 작업실에 등록 모델·장면·시험과
시간/HDR/DCG 실행이 보인다. `seed-r1-modes`는 패키지의 한정된 2파장 예제를
명시적으로 등록하며 같은 프로젝트에서 다시 실행해도 ID를 중복 생성하지 않는다.

정적 후보 비교는 같은 origin의 **설계 결정 비교**에서 연다. 등록 모델과 장면,
노출·gain·신호/SNR/포화 제한, seed를 선택한 뒤 실행한다. 적합 후보가 없거나
실패하면 성공한 선택으로 표시하지 않는다. 완료 사례의 원본 정수 RAW와 원본 ISP
RGB, 평가 전 고정 선택과 평가 결과를 저장·재열기한다.

새 RC2 사례의 `RAW 보기`에서 **저장된 RAW에서 ISP 재처리**를 열고 WB·CCM·tone을
조정한다. 별도 결과의 RGB와 원본 RGB를 나란히 보고 차이를 확인한 뒤 저장
목록에서 다시 열거나 JSON을 내보낸다. 원본 RAW·원본 RGB·후보 선택은 바뀌지
않고, 새 촬영은 0회다. RC1에서 만든 사례는 완전한 RAW 형식·좌표 정보가 없으면
조회만 가능하며 재처리를 거부한다.

선택형 시간/HDR/DCG는 `실험` 화면에서 등록된 Assembly와 해당 Scene Sequence,
Control Sequence, Test, 필요한 Capture Program을 **같은 seed 출력의 ID**로
맞춰 고른 뒤 실행한다. 시간 촬영은 `temporal_scene_v1`, 정적 독립-reset HDR는
`hdr_bracket_v1`, 공유 전하 DCG는 `shared_charge_dcg_v1`이다. 4–8 ms, 25°C,
gain 1의 16×16·500/600 nm 등록 조합만 파일럿으로 검증했다. HDR의 4 RAW는
서로 다른 reset 취득, DCG의 4×2 RAW는 취득마다 같은 전하의 두 readout이다.
PWM/LFM·일반 3D 움직임·자유 센서 조합은 이 설치 예제의 지원 범위가 아니다.

## 3. 백업·복구

서버를 중지한 뒤 R1 state 전체를 백업한다. 복원은 존재하지 않는 새 state 경로로
수행하고 **같은 프로젝트 ID**를 전달한다. 복원 대상에 먼저 `init-r1`을 실행하지
않는다.

```sh
R1_ARCHIVE="<new backup zip absolute path>"
R1_RESTORED_STATE="<new, nonexistent restore destination absolute path>"
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

복원 서버에서 저장한 실험·ISP 재처리·시간/HDR/DCG 결과와 artifact를 다시 연다.
RC2 설치 검증은 이 흐름과 원본 해시의 동일성을 확인한다.

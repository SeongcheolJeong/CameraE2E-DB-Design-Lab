# 새 설치의 가상 실측 예제와 재계산

예제는 두 가지 용도를 구분합니다.

| 경로 | 내용 | 현재 DB/검증 상태 변경 |
| --- | --- | --- |
| 보존 결과 열람 | 기존 합성 RAW·ISP 12쌍, 일반/HDR 보정 전후, 112 edge SFR, Color·Noise·Tone, CSV·ZIP | 없음. 읽기 전용 파일 카탈로그에만 표시 |
| 새로 계산 | 새 DB source → 새 seed의 개발 촬영 → 후보 고정 → 그 후 새 독립 검증 촬영 → A/B 보고서 | 새 작업·후보를 생성. DB 원본·활성 모델 자동 교체 없음 |

## 1. 기존 결과를 바로 열기

DB 복원이나 ISET 자산 준비 없이, Python API 실행 환경과 예제 팩만으로 열람할 수 있습니다.

```sh
mkdir -p downloads
gh release download esfr-examples-v1 \
  --repo SeongcheolJeong/CameraE2E-DB-Design-Lab \
  --pattern esfr-normal-hdr-examples-v1.tar.gz --dir downloads
python tools/install_esfr_examples.py downloads/esfr-normal-hdr-examples-v1.tar.gz
python tools/serve_db_design_lab.py --port 8781
```

`http://127.0.0.1:8781/examples/` 또는 DB Design Lab/E-SFR 화면의 **가상 실측 예제**를 엽니다. 설치기는 압축파일 및 모든 파일의 SHA256·크기를 검증하고 기존 예제를 덮어쓰지 않습니다. 읽기 전용 카탈로그는 예제 설치를 표시하지만 과거 workflow·검증 계약·active profile을 현재 Registry에 등록하지 않습니다. 보고서도 파일 해시가 바뀌면 제공하지 않습니다.

팩에는 12쌍의 원본 RAW·TIFF·metadata, 일반/HDR 고정 IQ 프로파일과 원본 ZIP, 보정 전후 Reference ISP, 촬영 ISP, 112 edge 원본/재계산 RAW·ISP·곡선과 통합 비교 보고서가 있습니다. 원본 ZIP은 바이트 그대로 보존합니다. 열람용 HTML만 링크를 상대 경로로 바꾸고 보존 예제 배너를 추가했습니다. 변경 내역과 원본 해시는 `provenance.json`에 있습니다. 수치와 영상 바이트는 수정하지 않습니다.

과거 실행 ID는 출처로 남습니다. 현재 서버에 없는 과거 작업 링크는 활성화하지 않습니다. 저장된 검증 RAW는 이미 사용된 과거 자료이므로 새 독립 검증으로 재사용하지 않습니다.

## 2. 빈 작업실에서 새로 계산

오프라인 DB 작업은 이 checkout의 `.cache/upstream/isetcam/<고정 revision>`에서 자산을 확인합니다. `PYISETCAM_UPSTREAM_ROOT` 환경변수만 지정한 것은 이 경로의 준비 완료를 뜻하지 않습니다. 아래 `ensure_upstream_snapshot()`를 이 checkout의 Python 패키지로 실행하세요.

먼저 README의 DB Release를 설치하고 `platform`, `factory-rayoptics`, `factory-prysm` 의존성을 준비합니다. 기본 작업 저장소가 외장 T7를 찾지 않도록 전용 실행기를 사용합니다.

```sh
# 고정 upstream revision과 tarball SHA를 검사하여 ISETCam 자산 준비
python -c 'from pyisetcam.assets import ensure_upstream_snapshot; print(ensure_upstream_snapshot())'

# 터미널 A: 기존 서버와 다른 포트 및 폴더로 시작
python tools/serve_db_design_lab.py --port 8781

# 터미널 B: 과거 실행 ID 없이 새 Reference × IMX462 DB 계산부터 진행
python tools/run_esfr_hdr_ab_live.py \
  --base-url http://127.0.0.1:8781 --create-db-source \
  --output outputs/new-esfr-normal-hdr
```

새 DB source는 공통 엔진으로 계산한 Reference 렌즈와 IMX462 구조 모델 한 쌍입니다. 합성 카메라의 숨겨진 blur·색 정답을 DB 실측값으로 등록하지 않습니다. DB run은 조건부 설계 출처이며 합성 입력의 수치 정답을 대체하지 않습니다.

첫 광학 계산에는 시간이 필요합니다. `source.json`, 개발/검증 입력, 후보 고정 계약, 영수증과 결과를 출력 폴더에 남깁니다. 재실행은 같은 서버의 같은 폴더에서 완료된 영수증을 재사용합니다. 다른 상태 폴더/서버의 영수증 재사용이나 다른 DB source로의 변경은 거부합니다. POST의 불확실한 실패는 자동 재시도하지 않습니다.

이미 완료된 **현재 서버의** DB run을 선택할 수도 있습니다.

```sh
python tools/run_esfr_hdr_ab_live.py \
  --base-url http://127.0.0.1:8781 \
  --source-run run-현재실행ID --candidate candidate-01 \
  --output outputs/another-new-esfr-example
# 조건부 DB 실행에만 --condition condition-006 추가
```

`--source-json outputs/new-esfr-normal-hdr/source.json`으로 같은 선택을 명시할 수 있습니다. 옵션을 생략하면 새 DB source를 만듭니다. 단일 모드 도구 `verify_esfr_iq_live.py`도 같은 옵션을 지원합니다. 서버 상태 SQLite나 외장 디스크를 클라이언트에서 직접 읽지 않고 모든 작업·조회는 HTTP로 수행합니다.

## 검증 범위

보존 예제의 RAW MTF는 일반 2/336, HDR 5/336이 기준을 초과합니다. 기존 RAW RMSE 0.03 / matched ISP 0.05는 유지합니다. HDR ISP 336건은 고정 Tone 차이에 따른 진단이며 합격으로 세지 않습니다. Color·Noise·Tone 합격 판정은 보류합니다. 새 계산 결과는 seed가 달라 수치·실패 건수가 달라질 수 있으며 고정된 통과 건수를 요구하지 않습니다.

이 팩은 합성 동작 검증과 과거 결과 열람 자료입니다. 실제 촬영 검증·전체 HDR DR·ISO/Imatest 동등성·새 PC 전체 의존성 설치 검증을 증명하지 않습니다. 실제 촬영 화면은 원래의 입력 계약·역할·독립성 검사를 유지합니다.

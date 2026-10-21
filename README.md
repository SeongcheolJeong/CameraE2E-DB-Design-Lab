# CameraE2E DB Design Lab

비공개 DB Lens × Sensor Design Lab 소스 스냅샷입니다. **2026-10-21에 포트 8780이 사용하던 고정 소스**를 보존합니다. 원래 `pyisetcam` 프로젝트와 Git 이력은 변경하지 않습니다.

## 포함 기능

- 특허 렌즈 처방 × 센서 모델 선택, PSF·RAW·ISP 재시뮬레이션 및 비교
- 조리개·광량·노출·움직임·센서 위치·셔터 조건 비교
- Calibration Lab: QE·CCM, Dark·PTC, MTF·PSF 및 조건부 카메라 프로파일
- E-SFR: 112개 edge, Color·Noise·Tone·CCM, 실촬영 입력 계약 및 독립 검증
- 일반/HDR 모드 간 독립 합성 A/B 비교와 HTML·JSON·CSV·ZIP 보고서
- 공통 API·SDK·UI, 계산 모듈, 관련 도구·문서·시험

앱 내부 모듈이 서로 연결되어 있어 DB 화면 한 파일만 분리하지 않고 계산에 필요한 Python 패키지를 함께 보존합니다. 원본 소스별 SHA256과 출처는 [SOURCE_SNAPSHOT.json](SOURCE_SNAPSHOT.json)에 있습니다.

## 설치

Python 3.12 이상과 GitHub 비공개 저장소 읽기 권한이 필요합니다.

```sh
git clone git@github.com:SeongcheolJeong/CameraE2E-DB-Design-Lab.git
cd CameraE2E-DB-Design-Lab
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[platform,factory-rayoptics,factory-prysm]'

# DB는 같은 비공개 저장소의 Release에 첨부되어 있습니다.
mkdir -p downloads
gh release download snapshot-20261021 \
  --repo SeongcheolJeong/CameraE2E-DB-Design-Lab \
  --pattern 'db-design-lab-db-seed-20261021.tar.gz' --dir downloads
python tools/install_db_design_lab_seed.py downloads/db-design-lab-db-seed-20261021.tar.gz
```

DB 첨부파일은 카탈로그·렌즈 처방·센서 저장 모델과 그 입력 자산을 보존합니다. SHA256·파일 목록은 [DB_SEED_MANIFEST.json](DB_SEED_MANIFEST.json)에 있습니다. 설치기는 기존 DB를 덮어쓰지 않으며 압축파일과 모든 파일의 해시를 검사합니다. Git의 단일 파일 크기 제한을 넘는 카탈로그는 Git 트리 대신 Release 안에 있습니다.

## 실행

```sh
python -m camera_platform serve \
  --workspace "$PWD" --state-dir "$PWD/outputs/db-design-lab" \
  --port 8780 --empty
```

브라우저에서 `http://127.0.0.1:8780/db-design.html`을 엽니다. 이미 8780이 사용 중이면 기존 서버를 종료하지 말고 `--port 8781`로 실행합니다. 새 상태 폴더에서 시작하므로 과거 실행 ID·활성 프로파일·업로드 데이터는 들어 있지 않습니다. 새 DB 계산을 제출한 후 결과 화면의 Calibration/E-SFR 링크로 이동합니다.

일부 광학/색 계산은 고정 ISETCam 자산을 추가로 사용합니다. `pyisetcam.assets`의 고정 revision/hash로 내려받거나 `PYISETCAM_UPSTREAM_ROOT`로 준비된 동일 자산 폴더를 지정합니다. Meep·Mitsuba·별도 장치 Solver는 필요한 작업에 맞게 별도 설치해야 합니다. 이 스냅샷은 가상 환경이나 모든 운영체제의 설치 검증을 포함하지 않습니다.

## 문서와 시험

- [DB Design Lab](docs/camera-platform-db-design-lab.md)
- [Calibration Lab](docs/camera-platform-calibration-lab.md)
- [E-SFR Color·Noise·Tone](docs/camera-platform-esfr-iq.md)
- [일반/HDR A/B](docs/esfr-normal-hdr-ab.md)
- [기존 프로젝트 설명](docs/pyisetcam-project-readme.md)

```sh
python -m pip install -e '.[dev]'
python -m pytest -q tests/platform/test_db_design_job.py tests/platform/test_db_design_sensor.py
python -m pytest -q tests/test_esfr_iq_ab.py tests/test_esfr_iq_report.py
```

기존 문서의 `outputs/`, `reports/`, 외장 디스크 경로는 당시 로컬 실행의 근거 위치입니다. 이 저장소에는 로컬 계산 결과·실촬영 RAW/ISP 업로드·서버 상태·가상 환경·캐시·인증정보를 포함하지 않습니다. Release의 DB 자산에 있는 저장 응답은 모델 입력이며 카메라 실측 성능을 증명하지 않습니다.

합성 실행 성공과 실제 카메라 검증을 구분합니다. 이전 일반/HDR 독립 합성 실험의 RAW MTF 기준 초과(일반 2/336, HDR 5/336)는 통과로 바꾸지 않습니다. HDR ISP의 고정 Tone 차이는 진단으로 처리하며 Color·Noise·Tone의 합격 기준은 별도 선언이 필요합니다. 실제 카메라 성능·전체 HDR 범위·제품 정합성은 미확립입니다.

기존 코드의 [LICENSE](LICENSE)와 각 데이터·외부 자산의 출처 고지를 유지합니다. 비공개 저장소라는 사실이 외부 자산에 새 사용권을 부여하지는 않습니다.

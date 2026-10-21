# CameraE2E DB Design Lab

비공개 DB Lens × Sensor Design Lab 소스 스냅샷입니다. **2026-10-21에 포트 8780이 사용하던 고정 소스**를 보존합니다. 원래 `pyisetcam` 프로젝트와 Git 이력은 변경하지 않습니다.

## 포함 기능

- 특허 렌즈 처방 × 센서 모델 선택, PSF·RAW·ISP 재시뮬레이션 및 비교
- 조리개·광량·노출·움직임·센서 위치·셔터 조건 비교
- Calibration Lab: QE·CCM, Dark·PTC, MTF·PSF 및 조건부 카메라 프로파일
- E-SFR: 112개 edge, Color·Noise·Tone·CCM, 실촬영 입력 계약 및 독립 검증
- 일반/HDR 모드 간 독립 합성 A/B 비교와 HTML·JSON·CSV·ZIP 보고서
- 공통 API·SDK·UI, 계산 모듈, 관련 도구·문서·시험

앱 내부 모듈이 서로 연결되어 있어 DB 화면 한 파일만 분리하지 않고 계산에 필요한 Python 패키지를 함께 보존합니다. 최초 보존 소스의 SHA256과 출처는 [SOURCE_SNAPSHOT.json](SOURCE_SNAPSHOT.json), 후속 변경은 [SOURCE_SNAPSHOT_UPDATES.json](SOURCE_SNAPSHOT_UPDATES.json)에 있습니다.

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

## 가상 실측 예제 바로 보기

[예제 설치와 새 계산 안내](docs/esfr-portable-examples.md)를 제공합니다. 합성 RAW·ISP 12쌍과 보정 전후·112 edge·일반/HDR A/B 보고서는 별도 비공개 `esfr-examples-v1` Release에서 받습니다.

```sh
gh release download esfr-examples-v1 --repo SeongcheolJeong/CameraE2E-DB-Design-Lab --pattern esfr-normal-hdr-examples-v1.tar.gz --dir downloads
python tools/install_esfr_examples.py downloads/esfr-normal-hdr-examples-v1.tar.gz
python tools/serve_db_design_lab.py --port 8781
```

`http://127.0.0.1:8781/examples/`에서 봅니다. 예제는 읽기 전용 카탈로그이며 현재 DB의 검증/활성 모델로 등록되지 않습니다.

## 실행

```sh
python tools/serve_db_design_lab.py --port 8780
```

브라우저에서 `http://127.0.0.1:8780/db-design.html`을 엽니다. 이미 8780이 사용 중이면 기존 서버를 종료하지 말고 `--port 8781`로 실행합니다. 새 상태 폴더에서 시작하므로 과거 실행 ID·활성 프로파일·업로드 데이터는 들어 있지 않습니다. 새 DB 계산을 제출한 후 결과 화면의 Calibration/E-SFR 링크로 이동합니다.

일부 광학/색 계산은 고정 ISETCam 자산을 추가로 사용합니다. `pyisetcam.assets.ensure_upstream_snapshot()`로 고정 revision/hash를 검사하여 이 checkout의 기본 캐시에 준비합니다. 오프라인 DB 계산은 `PYISETCAM_UPSTREAM_ROOT` 환경변수만으로 준비되지 않으므로 [독립 예제 안내](docs/esfr-portable-examples.md)의 자산 설치 절차를 따르세요. Meep·Mitsuba·별도 장치 Solver는 필요한 작업에 맞게 별도 설치해야 합니다. 이 스냅샷은 가상 환경이나 모든 운영체제의 설치 검증을 포함하지 않습니다.

## 실측 카메라와 시뮬레이션 모델 선택

E-SFR 실측 화면(`/esfr-measured.html`)의 **실측 카메라 · 시뮬레이션 모델 선택**에서 제조사·모델·개체·렌즈·센서를 기록하고 완료된 DB 후보를 연결합니다. 개발 RAW/ISP를 먼저 등록·선택한 뒤 모델을 고정합니다. 새 UI 작업은 이 선택이 필수이며 기존 작업의 출처 연결은 그대로 읽힙니다.

- 카메라 ID, 모드, RAW 형식, native geometry, CFA, pitch, 노출·gain·온도·조명·조리개·ISP 계약을 고정하고 개발/검증 입력 불일치를 거부합니다.
- 선택 DB의 보존된 4-site native PSF를 실제 RAW/Reference ISP 예측에 사용합니다. 기하 모델과 회절 포함 모델을 구분하며, 별도 Gaussian 근사나 pixel aperture 중복 적용을 하지 않습니다.
- 현재 전송은 2.9 µm RGGB와 저장된 ADC에 한정됩니다. 한 DB 시야를 전영역에 동일 적용하는 가정 또는 112 ROI별 명시적 DB 시야 지정을 선택합니다. 실제 센서 시야 대응을 자동 추정하지 않습니다.
- 독립 검증 3쌍에서 **선택 DB 모델 / 관측 / 개발에서 고정한 보정 후 응답**의 112 edge RAW Gr·ISP Y MTF, 오차, HTML·JSON·CSV·ZIP을 자동 생성합니다. DB Design Lab에서도 해당 보고서로 연결됩니다.
- 개발 영상의 geometry/밝기를 고정한 상대 MTF 비교입니다. 광학 처방 자체의 재최적화, 절대 색·Tone·노이즈·조도 예측, 제조사 ISP 및 실물 장비 대응의 검증은 포함하지 않습니다. 기존 IQ 보고서의 Color·Noise·Tone·CCM 평가를 별도로 연결합니다. 후보는 자동 활성화하지 않습니다.

완료된 DB 실행의 `run_id`, `candidate_id`, 조건부 실행이면 `condition_id`를 JSON 파일로 저장하고 새 합성 통합 시험을 실행할 수 있습니다. RAW/ISP 6쌍을 새로 만들고, 검증 3쌍은 후보 고정 이후 생성합니다.

```sh
PYTHONPATH=src python tools/run_esfr_camera_model_live.py --base-url http://127.0.0.1:8781 --source-json source.json --output outputs/selected-camera-example
```

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

기존 문서의 `outputs/`, `reports/`, 외장 디스크 경로는 당시 로컬 실행의 근거 위치입니다. Git 트리에는 로컬 작업 상태·실촬영 RAW/ISP 업로드·가상 환경·캐시·인증정보를 포함하지 않습니다. 보존된 합성 예제 데이터·보고서는 별도의 비공개 예제 Release에 포함합니다. Release의 DB 자산에 있는 저장 응답은 모델 입력이며 카메라 실측 성능을 증명하지 않습니다.

합성 실행 성공과 실제 카메라 검증을 구분합니다. 이전 일반/HDR 독립 합성 실험의 RAW MTF 기준 초과(일반 2/336, HDR 5/336)는 통과로 바꾸지 않습니다. HDR ISP의 고정 Tone 차이는 진단으로 처리하며 Color·Noise·Tone의 합격 기준은 별도 선언이 필요합니다. 실제 카메라 성능·전체 HDR 범위·제품 정합성은 미확립입니다.

기존 코드의 [LICENSE](LICENSE)와 각 데이터·외부 자산의 출처 고지를 유지합니다. 비공개 저장소라는 사실이 외부 자산에 새 사용권을 부여하지는 않습니다.

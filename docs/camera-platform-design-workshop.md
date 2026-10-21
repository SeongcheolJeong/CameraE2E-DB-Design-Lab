# Lens · Pixel · ISP 설계 워크숍

기존 Virtual Lab에서 **설계 워크숍**을 열면 실제 처방 해석, CFA 구조 변경, 같은 RAW의 ISP 비교를 하나의 실행·저장 흐름에서 사용할 수 있다. 준비된 자산으로 진행하는 40분 설계 변수 탐색 워크숍이다.

## 데모 순서

1. 3분: 기준·변경 영상을 같은 native 배율로 보고 원인을 예상한다.
2. 10분: 앞면 곡률·공기 간격·stop·sensor Z·A4를 변경한다. 처방→광선/OPD/PSF→시야별 radial/tangential MTF→RAW를 확인한다. sensor Z 3점 sweep은 focus×field 비교다.
3. 10분: CFA 두께 0.9/1.0/1.1배를 비교한다. Passivation이 반대로 변해 총 높이를 유지하며, 층별 흡수→절대 QE→전자·RAW→고정 ISP 색·noise로 연결된다.
4. 10분: 같은 RAW에서 denoise/sharpen/LSC/WB/CCM/tone을 변경한다. Edge SFR만 보지 않고 overshoot·texture·시간 잡음·색차도 확인한다.
5. 7분: 참가자가 한 변수 또는 두 변수(최대25개 후보)를 고르고 새 실험을 저장한다. 자동으로 승자를 정하지 않는다.

## 실행

기존 플랫폼과 동일한 서버 및 Worker를 사용한다. 외장 SSD `/Volumes/T7`가 필요하며, `/Volumes/T7/CameraE2E` 아래에 RAW·캐시·결과를 보존한다. 이 프로젝트 전체에 **500,000,000,000 bytes** 예산을 적용하고 볼륨의 여유 용량과 별도로 검사한다. 동시 workshop 쓰기는 하나로 제한한다. 저장소 부족 시 정확도를 낮추지 않고 명시적 실패로 종료한다.

```bash
PYTHONPATH=src .venv-platform/bin/python -m camera_platform serve \
  --workspace /Users/seongcheoljeong/Documents/CameraE2E \
  --state-dir outputs/camera-platform-virtual-lab-cra-20261011 --port 8780 --empty

PYTHONPATH=src .venv-platform/bin/python tools/run_design_workshop_demo.py
```

화면: `http://127.0.0.1:8780/design-workshop.html`

`CAMERA_PLATFORM_WORKSHOP_ROOT`는 다른 명시적 프로젝트 저장소를 선택할 때만 지정한다. 앱 DB/코드는 내부 디스크, 큰 결과와 캐시는 SSD에 둔다. 기존 데이터를 자동으로 이동하거나 삭제하지 않는다.

## SDK / API

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780") as client:
    spec = client.design_workshop_catalog()["specification"]
    revision = client.create_design_revision(spec, name="Workshop baseline")
    job = client.submit_design_workshop(spec)
    result = client.wait(job["id"], timeout=7260)
    # ISP 재처리는 광학/센서 취득을 반복하지 않는다.
    isp = {**spec["isp"], "denoise": 0, "sharpen": 1.2}
    replay = client.replay_design_workshop(job["id"], isp)
    replay_result = client.wait(replay["id"], timeout=7260)
    sweep = client.submit_design_sweep(spec, [
        {"path": "pixel.cfa_thickness_scale", "values": [0.9, 1.0, 1.1]}
    ])
    sweep_result = client.wait(sweep["id"], timeout=7260)
    # Sweep 후보도 정확한 candidate ID를 지정해 같은 RAW로 재처리한다.
    replay = client.replay_design_workshop(
        sweep["id"], isp, source_candidate_id="candidate-00")
```

`GET /api/v1/design-workshop/catalog`, `GET/POST /revisions`, `GET/POST /runs`, `POST /sweeps`, `POST /replays`, `GET /runs/{id}`, `POST /runs/{id}/cancel`을 제공한다. 공통 `/api/v1/jobs/{id}`와 `client.wait()`가 같은 lifecycle을 사용한다. 성공 결과의 manifest와 다운로드 파일 해시, 재처리 원본 해시를 검사한다.

## 계산 계약

- 광학: authored aspheric singlet의 실제 RayOptics/Prysm 해석. 450–650nm, 10nm 간격, 동일550nm 기준 파장을 유지하는 batch 계산. 임의 blur kernel로 처방을 대신하지 않는다.
- 기본3.45µm pitch와 ±0.5mm field 지원에서 PSF halo를 제외한 실제 native crop을 사용한다. 기본값은138×248이며 확대 화면은 native 해상도로 오인하지 않도록 표시한다.120°·2MP parametric Lab은 별도 경로다.
- 수신 픽셀 적분 PSF를 사용하므로 후단 box/PD 개구를 다시 적용하지 않는다. 분광 source-cell 중심 근사와 수신측 pupil 평균 응답을 사용하며, landing position과 angle의 완전한 결합 수송은 아니다.
- 픽셀: IMX462 자료의 층·재질 proxy에서 파생한3.45µm **연구 셀**이다. 원본2.9µm 제품 구조나 제품 mode를 재현했다고 주장하지 않는다. CFA/passivation만 교환하고 총 높이·pitch·Si·readout은 고정한다.
- 절대 QE가 기존 saved QE를 **대체**한다. 일반 CFA·fill factor·QE를 다시 곱하지 않는다. OCL routing·DTI·전하 수집 구조 해석은 이번 범위에 없다. 수집효율과 readout은 선언된 공통 가정이다.
- RAW는 독립 shot/read noise를 재취득한다. 입력·평균 응답을 재사용해도 clipping된 RAW를 배율 조정하지 않는다. ISP 비교는 byte-identical RAW를 사용한다.
- 색 변환은 기준 셀·기준 처방의 D65에서 한 번 고정한다. 후보·프레임별 밝기 또는 색 재정규화가 없다. 색차는450–650nm 분광 기준의 모델 진단이다.
- 표시 범위는 `isp.display_white_level`로 명시한다(1–100, 기본1). 밝은 데모는10000 cd/m²·white level10을 모든 후보에 공통 적용한다. 이 값은 PNG 표시만 바꾸며 RAW·linear RGB·수치 지표를 바꾸지 않는다. 색차의 백색은 선언한 입력 광원·광량에서 가져오며 출력에 맞춰 추정하지 않는다.
- Noise는 동일 조건 반복 취득의 픽셀별 시간 분산이다. Spatial variation·texture를 시간 잡음으로 취급하지 않는다. SFR·색차·noise는 Lab 방식 진단이며 EMVA 인증이나 실측 검증이 아니다.

## 결과와 재사용

렌즈가 바뀌면 광학부터, CFA가 바뀌면 sensor response부터, ISP만 바뀌면 보존 RAW부터 계산한다. 캐시 키는 정규화된 설정·source code·응답과 연결하며 캐시 배열 해시가 맞아야 사용한다. 새 설계를 제출한 상태와 이전 결과를 분리하고, 완료된 run/candidate ID에 화면을 결합한다.

유리 재료 탐색, tilt/decenter 공차 수율, 실제120° 처방, 새로운3D FDTD/TCAD 캠페인, HDR 시퀀스 평가는 별도 후속 범위다.

## 준비된 실제 실행 — 2026-10-11

서버8780에서 다음 순서로 바로 시연할 수 있다. 모든 링크는 실제 완료된 run에 연결된다.

| 시연 | 열기 | 보여줄 내용 |
|---|---|---|
| Lens A/B | [조리개0.50→0.65mm](http://127.0.0.1:8780/design-workshop.html?run=run-9f56b167ab5348b8b5aecd7d531a51e0&baseline=run-264af64c70694aafaebbb70dfcf69b5d) | 동일 광량·노출·ISP에서 평균 전자288.71→488.72e. PSF·MTF·RAW를 함께 확인 |
| Focus×field | [센서 Z −20/0/+20µm](http://127.0.0.1:8780/design-workshop.html?run=run-3a6bbfc37cea42559bf7c8944c6f61e4&candidate=candidate-01&baseline=run-8b1131166e4e4870b9d9c2f87c2a21f1) | 3초점×9시야, radial/tangential, 주파수별 실제 MTF. 작은 차이를 과장하지 않는 고정색상 범위 |
| Pixel A/B | [밝은 조건 CFA3구조](http://127.0.0.1:8780/design-workshop.html?run=run-862a7ac3b97d40d09157182dbedf65c6&candidate=candidate-00&baseline=run-264af64c70694aafaebbb70dfcf69b5d) | 두께·층별 흡수·QE·색 누설과 RAW·ΔE 비교 |
| ISP noise/detail | [동일 RAW denoise](http://127.0.0.1:8780/design-workshop.html?run=run-1c369842a02242939cf1e973ef5e04b0&baseline=run-8b1131166e4e4870b9d9c2f87c2a21f1) | 처리후 시간잡음σ0.05077→0.01999, texture contrast ratio1.085→0.454. 잡음 이득과 질감 손실 동시 표시 |
| ISP sharpen | [동일 RAW sharpening](http://127.0.0.1:8780/design-workshop.html?run=run-de88abdbcabd4874afcb18480c53d5c0&baseline=run-8b1131166e4e4870b9d9c2f87c2a21f1) | SFR·overshoot·잡음 증가를 함께 확인 |

조리개 변경의550nm 중심 MTF@0.125 cycles/native-pixel은0.508→0.576이다. 색차는1.89→11.41로 증가한다. 기준 노출·색 변환을 유지했기 때문에 밝기 변화도 ΔE에 포함된다. 이를 색 정확도 개선이나 후보의 종합 우위로 해석하지 않는다.

실제 처방4개의 최초 해석은 각각 약357–371초였다. 검증된 광학 캐시를 사용한6종 chart·48RAW 취득/ISP는 약5–6.5초, 같은 RAW의 ISP 재처리는 약1.2초였다. API 대기·파일 검증·화면 로딩을 포함한 사용자 대기시간과는 구분한다.

인도 시점 프로젝트 SSD 사용량은 약2.34GB/500GB(원본 ZIP 포함)다.9개 실행 묶음의4,794개 파일 해시, SDK 재처리2개와 브라우저 후보 재처리1개의 RAW 동일성을 확인했다. Workshop·Virtual Lab 관련136개 테스트가 통과했다. 실제 브라우저의 revision 복원·후보 전환·ISP 제출·초점맵 연결도 확인했다.

재확인:

```bash
PYTHONPATH=src .venv-platform/bin/python tools/verify_design_workshop_demo.py
PYTHONPATH=src .venv-platform/bin/python -m pytest \
  tests/platform/test_design_workshop*.py tests/platform/test_virtual_lab*.py -q
```

실행 설정/ID는 `outputs/design-workshop-delivery-20261011/runs.json`, 파일·RAW 확인 결과는 같은 디렉터리의 `verification.json`, 실제 브라우저 기록은 `outputs/design-workshop-demo-20261011/ui-browser-qa.json`에 있다. 화면의 **원본 ZIP**으로 선택한 실행의 설정·원본·보고서를 묶어 내려받을 수 있다. 원본 DB/재질 자산의 외부 재배포 권한은 실행 성공과 별개다.

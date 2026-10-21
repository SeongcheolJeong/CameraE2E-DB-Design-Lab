# Reference ISP Lab

Reference Bench의 특허 렌즈·검토된 센서 구조에서 생성한 **동일 RAW를 다시 처리**하여, 광학·픽셀 설계와 ISP의 상충관계를 비교한다. 기존 `/reference-workshop.html`과 Registry·Worker·SDK를 사용한다. 새 렌즈 또는 센서 체계를 만들지 않는다.

2026-10-12 확장인 [ISP 파라미터 튜닝](camera-platform-reference-isp-tuning.md)은 LSC 강도·상한, RAW NR, bounded sharpen의 명시적 후보 탐색과 한 프레임 preview, 시간 NPS, 밝기 제약을 추가한다. 실제 12후보 평가와 저장 프로필 재실행 결과는 해당 문서에 별도로 기록한다. 아래 2026-10-11 인도 기록은 그 당시 실행의 근거로 유지한다.

## 데모에서 보여줄 세 가지 질문

| 비교 | 고정하는 것 | 실제 바꾸는 것 | 함께 볼 결과 |
| --- | --- | --- | --- |
| 주변부의 잡음을 줄이면서 질감을 유지할 수 있는가? | 9개 시야의 RAW·고정 LSC·색·표시 범위 | 같은 CFA 사이트 RAW NR 0/0.4/0.8 × 제한된 sharpen 0/0.3, 총 6개 | 시간 잡음, 질감 유지, edge overshoot/undershoot, 색차, 제약별 탈락 이유 |
| CFA 변경 후 색 보정도 다시 설계해야 하는가? | 각 CFA 후보 안에서는 동일 RAW·LSC·수동 WB/CCM | 기준 spectral CCM과 해당 광학·픽셀 응답으로 재피팅한 CCM | 보류한 Macbeth24의 모델 색차, 실제 취득 차트 색차, RGB 시간 잡음, 행렬 계수 크기 |
| 렌즈를 거친 입력에서 demosaic가 고주파 정보를 어떻게 보존하는가? | 기준 특허 렌즈의 동일 chirp RAW·LSC·색·표시 범위 | bilinear / laplacian / adaptive_laplacian | 고주파 색 잔차와 luma 오차, 알고리즘별 실제 처리 영상. 곡률 ×1.005 비교는 선택 확장 |

한 알고리즘을 모든 조건의 승자로 표시하지 않는다. 잡음·질감 recipe만 사전에 고정한 제약 안에서 후보를 선택하며, 색·demosaic recipe는 비교 결과를 제시한다.

## 처리 계약과 기존 코드 재사용

`camera_reference_isp_v2`는 기존 Reference ISP 설정과 구분되는 명시적 schema다. 기존 설정은 기존 경로로 실행한다. `reference_isp.default_profile(legacy)`는 WB·CCM·tone·표시 범위·LSC 설정을 옮기되, 기존 Gaussian denoise나 sharpen 값을 새 알고리즘 강도로 해석하지 않는다.

새 경로는 다음 순서로 계산한다.

```text
native 정수 RGGB RAW
  → black pedestal 제거 (추가 black_offset_dn은 0으로 고정)
  → normalized DN
  → 고정된 LSC 한 번 적용
  → 같은 CFA 사이트의 잡음 적응 RAW NR
  → 전자 단위로 변환
  → 선택한 Bayer demosaic
  → 고정 electron-to-linear-sRGB 행렬
  → 수동 WB·CCM
  → 표시 white 기준으로 제한한 선형 detail 보정
  → 공통 display_white_level·tone_gamma
```

- RAW NR와 제한된 detail 보정은 `isp_v2.py`에서 추출한 순수 함수를 공유한다. 기존 v2/v3 처리 의미는 유지한다.
- `pyisetcam.bayer_demosaic.demosaic_bayer()`는 기존 Bayer 구현을 엄격하게 호출한다. 지원하지 않는 조합을 다른 알고리즘으로 조용히 바꾸지 않는다. 이번 Reference 입력은 로컬 원점 `[0,0]`의 RGGB다.
- 색 행렬 입력은 **전자**, 출력은 부호를 유지한 선형 sRGB다. `processed.npy`를 프레임 최대값으로 정규화하지 않는다. 표시만 고정 white·gamma를 사용한다.
- 현재 연결은 선형 readout, PRNU/DSNU 0, digital gain 1인 Reference readout 범위다. 저장 모델의 제약을 풀어 일반 비선형 센서 지원으로 표시하지 않는다.
- LSC가 잡음도 증폭하므로 NR의 분산 hint에도 LSC gain²을 반영한다. 분산 hint는 ADC 양자화·shot/read noise 모델의 근사이며, 처리 후 잡음 결과는 반복 RAW의 실제 시간 분산으로 계산한다.

원본 DN, normalized DN, 전자, 선형 RGB의 단위가 다르므로 서로 같은 범위의 영상으로 취급하지 않는다. 결과 화면의 단계 이름과 고정 표시 범위를 확인한다.

## 동일 RAW와 평가 지지 영역

각 후보는 원본 `capture.json`, RAW, 평균 전자율, 광학·픽셀 응답, LSC calibration을 보존한다. source run·candidate·manifest SHA-256을 작업 명세에 기록한다. 재처리는 취득 설정을 바꿀 수 없으며, 원본 hash가 달라지면 거부한다.

9개 시야는 각각 **256×256 native ROI**다. 2.9 µm pitch를 유지하며 별도의 광학 주변 입력을 생성한다. 제품 센서의 전 프레임 모드나 전체 사진이라고 표시하지 않는다.

RAW 반복의 rail 유효성으로 정한 동일 마스크에 8 pixel의 공통 ISP 지지 영역을 적용한다. 질감 highpass 지표에는 추가 4 pixel 지지 영역을 적용하고, 색 패치·edge의 실제 측정 ROI가 마스크 안에 모두 들어가는지 확인한다. `tests.isp_lab.metric_support`에 색·해상력·질감의 사용 가능 여부와 이유를 저장한다. 후보마다 유리한 픽셀만 다시 고르지 않는다. 반복 RAW의 공간 질감을 시간 잡음으로 계산하지 않으며, RGB와 luma의 시간 표준편차를 기록한다. 후보 하나라도 필수 시야 지표나 그 지지 영역이 없으면 제약 선택에서 통과시키지 않는다.

잡음·질감 기본 선택 조건은 다음과 같다.

- 모든 시야의 질감 유지 비율 ≥ 0.95.
- 기준 ISP 대비 최대 추가 halo ≤ 0.05.
- 기준 ISP 대비 최대 ΔE00 증가 ≤ 1.0.
- 위 조건을 만족한 후보 중 **가장 나쁜 시야의 시간 잡음**이 최소인 후보를 선택한다.
- 잡음 개선이 1% 미만이면 기준 ISP를 유지한다. 기준안의 필수 지표가 불완전하면 미결정이다.

이 수치는 데모의 고정 조건이며 보편적 제품 규격이 아니다. 후보가 제약을 넘으면 그 이유를 남기고 기준안을 유지할 수 있다.

## 색 보정과 demosaic의 기준

색 재피팅은 보존된 광학·픽셀 응답의 실제 중심 동공 적분을 사용한다. D65·기준 노출의 Munsell64 반사율로 white-constrained least squares를 피팅하고, 피팅에 넣지 않은 Macbeth24로 모델 색차를 평가한다. 고정 자산은 로컬 ISET 반사율 파일 `MunsellSamples_Vhrel.mat`와 `macbethChart.mat`이며 450–650 nm/10 nm로 사용한다. 학습/보류 자산의 파일·재표본화 내용 hash를 저장하고 동일 스펙트럼의 중복을 검사한다.

행렬은 `reference-isp-color-<content hash>` ID를 갖는다. 광학 revision·픽셀 응답·readout과 결합되므로 다른 CFA에 무조건 재사용할 수 없다. 한 중심 행렬을 모든 시야에 그대로 적용하며, 실제 평가 RAW를 사용한 피팅이나 프레임별 밝기 보정은 하지 않는다. 보류 반사율의 이상적 모델 색차와 실제 잡음·공간 처리까지 거친 취득 차트 색차를 분리한다.

`isp_lab_v1` 차트에는 주파수 norm이 0.02→0.45 cycles/native pixel로 변하는 45° analytic chirp가 추가된다. 같은 위상을 광학 halo까지 연장하여 입력을 만들며, 저해상도 결과를 나중에 확대하지 않는다. native cell 중심 샘플링의 광학 수렴이 검증되었다는 의미는 아니다.

chirp 취득에는 R/Gr/Gb/B의 co-located 평균 전자율 plane을 보존한다. 이를 mosaic 평균 응답과 대조하고, CFA 샘플링 손실이 없는 동일 모델 기준과 demosaic 출력을 비교한다. 이 진단에서는 잡음·ADC를 우회한다. 실제 RAW 처리 영상과 함께 보되, 이 기준을 측정 정답이나 광학 단독 MTF라고 부르지 않는다.

## 공개 SDK로 실행

서버가 실행 중이고 외부 프로젝트 저장소가 연결된 상태에서 사용한다. 재처리 입력은 완료된 정확한 source ID이며, sweep의 경우 candidate ID도 필요하다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=120) as client:
    source_id = "run-34aadb199e4d416bb0ba390ccf79636c"
    job = client.submit_reference_isp_study(source_id, "noise_detail")
    finished = client.wait_reference_workshop(job["id"], timeout=7260)
    if finished["status"] != "succeeded":
        raise RuntimeError(finished.get("error", finished["status"]))
    summary = finished["result"]["summary"]
    print(summary["study"]["selection"])
    print(f"http://127.0.0.1:8780/reference-workshop.html?run={job['id']}")

    # 동일 source로 색 calibration fitting + 두 ISP 재처리.
    color = client.submit_reference_isp_study(source_id, "color_recalibration")
    color_result = client.wait_reference_workshop(color["id"], timeout=7260)
```

`demosaic` recipe는 `chart_suite="isp_lab_v1"`로 새로 취득한 source가 필요하다. 과거 4차트 source에 chirp를 결과만 만들어 붙이지 않는다. 새 취득을 한 번 완료하면 이후 3개 demosaic 후보는 같은 RAW를 재사용한다.

기존 sweep candidate의 예:

```python
job = client.submit_reference_isp_study(
    "run-c07bf344f52848be9727c122ccde8257",
    "color_recalibration",
    source_candidate_id="candidate-00",  # CFA ×0.9
)
```

작업 제출은 `POST /api/v1/reference-workshop/isp-studies`다. 조회·공통 wait·cancel은 기존 Reference lifecycle을 사용한다. 취소는 `client.cancel_reference_workshop(job_id)`로 요청한다. `constraints=`로 선언한 선택 조건을 바꿀 수 있지만, 결과를 받은 뒤 선택 조건을 바꾸면 새로운 연구 설정으로 구분해야 한다.

## 준비·검증·결과 파일

다음 실행기는 잡음·질감 1개, CFA 세 배율의 색 비교 3개, 기준 특허 렌즈의 chirp 취득 1개와 demosaic 비교 1개를 준비한다. **기본 계획은 6개 작업·16개 후보**이며, 실제 완료 수는 verification 파일을 기준으로 한다. `--include-lens-variant`를 지정하면 곡률 ×1.005의 취득·demosaic 2개 작업·4개 후보를 추가한다. 이 확장을 기본 인도 완료 조건으로 요구하지 않는다.

```bash
PYTHONPATH=src .venv-platform/bin/python tools/run_reference_isp_lab_demo.py
PYTHONPATH=src .venv-platform/bin/python tools/verify_reference_isp_lab_demo.py
```

- 작업 영수증: `outputs/reference-isp-lab-delivery-20261011/runs.json`.
- 검증·간결한 수치·선택 결과: 같은 폴더의 `verification.json`.
- 원본과 처리 결과: `/Volumes/T7/CameraE2E/reference-workshop/<run-id>`.
- 각 차트: 원본 RAW, 평균 응답, 선형 결과, 표시 영상, `isp-stages/manifest.json`, 단계별 배열, 공통 유효 마스크.
- 색 fitted 후보: `color-calibrations/<id>.json`.
- chirp: `planes-electron-rate.npy`, `demosaic-reference.npy`, `demosaic-residual.npy`.

검증 도구는 각 label의 최신 완료 영수증과 서버 결과·현재 구현 fingerprint를 대조하고 SSD의 manifest 파일을 모두 hash 검사한다. 원본 경로는 한 번만 hash하여 재사용하고, 각 결과 사본은 별도로 검사한다. 6/3/2개 recipe 설정, 9개 시야, RAW 보존, 색 학습/보류 분리와 white 제약, chirp basis, 지표의 실제 지지 영역, 고정 표시 범위, 저장된 선택 결과를 확인한다. 이전에 실패·취소되거나 새 작업으로 대체된 영수증은 `unused_receipts`에 남긴다. API 조회와 파일 읽기만 수행하며 새 촬영·피팅·광학 계산은 제출하지 않는다. 최신 작업의 누락·실패·구현 불일치는 통과 처리하지 않는다.

### 실제 인도 기록

2026-10-11 실제 실행에서 **6개 작업·16개 후보·144개 시야 결과**가 완료되었다. 최신 구현 fingerprint로 manifest 파일 **46,753개**, native 정수 RAW 사본 **4,896개**를 검사했고 최종 검증 상태는 `verified`다. 논리 용량은 **9,057,386,690 bytes (9.057 GB)**, SSD 할당 용량은 **14,684,913,664 bytes (14.685 GB)**다. 이번 결과 manifest 범위이며 source 전용 작업·광학 캐시는 제외한다. 동일 RAW를 보존한 사본 수를 독립 촬영·장면 수로 세지 않는다. 선택 확장인 곡률 ×1.005의 새 chirp/demosaic 작업은 이 인도에 포함되지 않는다.

- [완성 보고서](../outputs/reference-isp-lab-delivery-20261011/report.html)
- [전달 명세](../outputs/reference-isp-lab-delivery-20261011/delivery.json)
- [전체 검증·수치](../outputs/reference-isp-lab-delivery-20261011/verification.json)

| 데모 | 실제 결과와 보여줄 상충관계 |
| --- | --- |
| [같은 RAW의 잡음·질감 후보](http://127.0.0.1:8780/reference-workshop.html?run=run-0f6e9c1063b34647bcbdc005eee1ceac&candidate=candidate-04&baseline=run-0f6e9c1063b34647bcbdc005eee1ceac&baseline_candidate=candidate-00&field=outer-ne) | NR 0.8에서 최악 시야의 시간 잡음 0.007858→0.005499, 약 30.0% 감소. 최소 질감 유지가 85.64%여서 사전 95% 조건을 넘지 못했다. 자동 선택은 **기준 ISP 유지**다. |
| [CFA ×0.9의 고정 CCM·재피팅 비교](http://127.0.0.1:8780/reference-workshop.html?run=run-e6c237d2a04d432b84f34cbc3a7f27c5&candidate=candidate-01&baseline=run-e6c237d2a04d432b84f34cbc3a7f27c5&baseline_candidate=candidate-00&field=outer-ne) | 주변 NE의 취득 차트 ΔE00 3.710→2.551, 색 차트 시간 잡음 Y 0.009262→0.009034. ×1.1에서는 ΔE00 3.123→2.583으로 줄지만 잡음은 약 4.88% 증가한다. ×1.0에서는 재피팅이 색차를 2.440→2.568로 악화시킨다. 재피팅의 보편적 우월성을 주장하지 않는다. |
| [기준 특허 렌즈에서 3개 demosaic 비교](http://127.0.0.1:8780/reference-workshop.html?run=run-f965770b9132439986529c7c5fc2699b&candidate=candidate-02&baseline=run-f965770b9132439986529c7c5fc2699b&baseline_candidate=candidate-00&field=outer-ne) | 주변 NE의 chirp chroma RMS는 bilinear 0.034151, laplacian 0.031623, adaptive_laplacian 0.027880이다. 잡음·ADC를 우회한 이 모델 기준에서는 adaptive가 bilinear보다 약 18.36% 작다. 실제 RAW 처리 영상과 모델 잔차를 구분해 볼 수 있다. |

[이전 Gaussian과 새 RAW NR의 동일 원본 비교](http://127.0.0.1:8780/reference-workshop.html?run=run-0f6e9c1063b34647bcbdc005eee1ceac&candidate=candidate-02&baseline=run-cbdb6ba5d2804c58a97c37ea23c0bb08&baseline_candidate=candidate-00&field=outer-ne)도 제공한다. 기존 Gaussian 지표의 whole-ROI 영역은 새 지지 마스크와 다르므로, 두 숫자를 같은 기준의 순위로 합치지 않는다.

| 작업 | 실제 작업 시간(s) | 실행 전 대기(s) |
| --- | ---: | ---: |
| 잡음·질감 6개 후보 | 332.548 | 0.242 |
| CFA ×1.0 색 비교 | 122.447 | 0.021 |
| CFA ×0.9 색 비교 | 140.080 | 92.815 |
| CFA ×1.1 색 비교 | 147.025 | 0.071 |
| 기준 chirp 취득 | 250.337 | 0.128 |
| 3개 demosaic 재처리 | 236.500 | 0.025 |

작업 시간은 `groups[].timing`에 기록하며 로컬 Registry의 hash 검증된 `runs.running`→`runs.succeeded` 시각 차이를 사용한다. 취득·ISP·파일 저장·완료 처리를 포함한 합계는 **1,228.938초**, 별도 `created`→`running` 대기 합계는 **93.302초**다. 후보별 시간이 없으면 작업 시간을 후보 수로 나누어 만들어내지 않는다. 검증 도구의 `--state-dir` 기본값은 `outputs/camera-platform-virtual-lab-cra-20261011`이며 SQLite를 읽기 전용으로 연다. 준비된 결과 선택 속도와 새 계산 시간은 구분하며, 이 벽시계 시간을 ISP 하드웨어 지연으로 사용하지 않는다.

### 실행 환경과 화면 확인

현재 8780 서버는 `.venv-platform/bin/python`의 editable source와 `outputs/camera-platform-virtual-lab-cra-20261011` 상태를 사용한다. 수치 코드 fingerprint는 `0af8a035aad7483fc92a66b6035c255c42502297c7cd7ded68ad6944ec8cc3eb`다. 관련 Python 회귀 278개와 브라우저 VM 2개가 통과했다.

실제 화면에서 후보 변경·동기 확대·처리 단계·색 CCM A/B·chirp 잔차·원본 다운로드를 확인했다. 선택된 기준 ISP는 `reference_design_revision-1a0d63fecf744dfea419bc48ad4be542`로 저장했다. 화면에서 이 설정을 불러와 제출한 재처리 `run-f019925b03a745fa920fc72457ba9fa3`는 성공했고, RAW 288개 hash와 처리 배열 36개가 원래 선택 후보와 같았다. 증거는 [UI 확인 기록](../outputs/reference-isp-lab-delivery-20261011/ui-verification.json)에 있다.

전체 카탈로그 조회는 준비 결과·저장 예산을 확인하므로 느릴 수 있다. 직접 결과 링크는 해당 실행을 병렬로 먼저 열어 영상·지표를 보여준다. 카탈로그 준비 전에는 편집·제출을 잠그며 준비 후에도 선택한 후보·시야·단계를 유지한다. 실제 재로딩에서 약 6.2초 시점에 native 영상이 표시되고 카탈로그는 계속 로딩 중인 상태를 확인했다. 이는 한 번의 UI 관측이며 처리율 보증이 아니다.

기하 ray PSF는 회절을 포함하지 않는다. CFA 구조의 재료·수집·readout 가정도 유지한다. 이 데모는 구현된 통합 처리와 모델 내 상대 차이를 보여주며, 제품 센서의 실측 보정이나 품질 인증을 대신하지 않는다. 기존 Reference 출처와 원자료 이용 조건은 [Reference Bench 문서](camera-platform-reference-workshop.md)를 따른다.

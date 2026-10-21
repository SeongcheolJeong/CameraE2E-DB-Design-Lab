# 같은 RAW의 ISP 후보 탐색 · v0.16.0.dev6

개발 소스의 `camera_isp_study_v1`은 **보존한 native RAW와 보정 버전을 고정한 채
ISP 후보를 비교하고, 선택 이유와 별도 자료의 검증 결과를 사례에 남기는 기능**입니다.
기본 설치본 v0.15에는 아직 포함하지 않았습니다. 개발 소스 구현과 실제 API·Worker·화면·
재시작 검증은 구분하며, 실행 근거는 [ISP Study 보고서 디렉터리](../reports/camera_platform/isp-studies-v016/)에서 확인합니다.
실제 HTTP 실행은 tuning 18개·별도 검증 6개, 화면에서는 추가 edge 후보 2개의 제출·결과·
프레임 비교·선택 및 사례 r2 저장을 확인했습니다. 재시작 후 기존 결과의 재조회도 통과했습니다.
259개 Python 시험과 41개 Node 상태 시험은 이 범위의 구현 검증이며, 설치 전환을 뜻하지 않습니다.

[dev5 고급 ISP](camera-platform-advanced-isp.md)의 단일 RAW 재처리를 확장합니다.
당시 [독립 비교 도구](../tools/verify_camera_isp_tradeoffs.py)의 합성 실험은 도구 실행
근거이며, dev6의 Study 등록·별도 Worker·사례 연결을 실행한 근거로 대신하지 않습니다.
[이전 next-slice 문서](camera-platform-isp-studies-next-slice.md)는 당시의 미구현 제안으로 보존합니다.

## 무엇을 비교하는가

예를 들어 “잡음을 줄일 때 질감 진폭 손실과 edge halo를 어디까지 허용할 것인가?”를
질문으로 잡습니다. 반복 flat·edge·texture 자료에 같은 후보들을 적용하고 각 지표를
따로 비교합니다. RAW를 다시 촬영하거나 노출·gain·광학·픽셀 응답을 바꾸지 않습니다.

```text
고정 RAW + 고정 calibration + 기준 v2 ISP
  → 명시한 denoise/sharpen 값의 유한 조합 + 기준 후보
  → 후보 × 데이터셋 Worker 작업 → 고정 ROI의 지표·제약·표본 Pareto
  → 사용자의 후보 선택과 이유 → 새로운 자료의 독립 검증 → 사례 판단·다음 시험
```

탐색 축은 `raw_denoise.strength`, `linear_sharpen.amount` 두 가지이며 값은 [0, 1]입니다.
LSC·noise floor·sharpen threshold/max_delta와 BLC·demosaic·WB·CCM·tone은 기준
프로파일 그대로 유지합니다. 해당 단계가 bypass이면 그 축은 계획 검사에서 거부합니다.
기준 v2 후보는 항상 포함하며 정규화한 프로파일이 같으면 한 후보로 합칩니다.
각 작업에는 공통 BLC·demosaic·WB·CCM이 같은 **v1 비교 영상**도 별도로 남습니다.
기준 v2 후보와 v1 비교 영상은 서로 다른 개념입니다.

## 입력과 자원 경계

| 항목 | 현재 계약 |
|---|---|
| 원본 | 등록된 `run` 또는 `capture`의 단일 stream integer native RAW, F×H×W |
| 반복 | 데이터셋마다 4–16 frame, 정지 장면과 같은 취득 설정의 반복 |
| 좌표 | 원래 full sensor shape·crop 원점·CFA 위상·pixel pitch·frame 순서 보존 |
| 보정 | 전체 Study에서 하나의 불변 calibration과 그 ID를 고정한 기준 v2 ISP |
| 데이터셋·축 | 데이터셋 1–12개, 탐색 축 최대 2개, 축당 서로 다른 값 1–12개 |
| 전체 작업 | 기준 후보 포함 **후보 × 데이터셋 24개 이하** |
| 지표 집계 | 목적 1–4개, 제약 최대 12개; 각 집계의 데이터셋과 단위를 명시 |
| 배열·연산 | 기존 replay의 배열별 128 MiB, working array 추정 1 GiB 및 frame-pixel 제한 유지 |
| 근거 예산 | 입력 계보·출력·manifest를 포함한 보수적 비압축 추정 **256 MiB 이하**; export 시 실제 크기 재검사 |

자원 예산은 peak RSS나 실시간 처리 보증이 아닙니다. 초과 입력을 자동 축소하지 않으며,
전체 계획을 검사한 뒤 한 묶음으로 등록합니다. 기준 후보와 여러 데이터셋을 추가하면
각 축의 개별 상한보다 먼저 24개 작업 또는 근거 예산에 도달할 수 있습니다.

Spatial/Temporal/HDR/Closed-loop 원본도 허용된 native 취득 계약에서 **고정 설정의
반복임을 확인할 수 있을 때**만 사용합니다. 서로 다른 노출의 HDR bracket, 바뀌는
AE/AWB 설정, 시간에 따라 바뀌는 장면, 주기 조명은 이 비교에 사용하지 않습니다.
대표 격자 영상, DCG의 두 readout, float HDR 합성 결과, 후보가 만든 RGB를 RAW 원본으로
받지 않습니다. DCG stream을 반복 frame으로 펼치는 추출 기능도 제공하지 않습니다.

## 고정 ROI와 지표의 의미

모든 수치 지표는 tone 이전의 signed linear RGB에서 계산합니다. 후보는 저장한
`sharpened_rgb_frames`, v1 비교는 `ccm_rgb_frames`를 사용합니다. 표시 PNG와 tone
이후의 uint8 RGB는 수치 입력이 아닙니다. RGB의 산술 평균에 `(white − black)`을
곱한 guide 단위는 `DN_equivalent`이며 휘도·전자 수·광자 수가 아닙니다.

ROI는 해당 native crop 안의 row/column 좌표이며 끝 좌표는 포함하지 않습니다.
계획에서 모든 가장자리의 4 native pixel을 제외한 고정 mask를 만들고 ROI와의 교집합을
평가합니다. 이 영역이 비면 제출 전에 거부합니다. 후보마다 실제 RGB validity를 별도로
확인하며, 선택 영역에 무효 표본이 있으면 지표는 `unknown`입니다. 결과가 좋아지도록
무효 pixel을 사후 제외하거나 후보마다 ROI를 줄이지 않습니다.

| 지표 | 해석 |
|---|---|
| `temporal_residual_std_dn` | pixel별 반복 평균에 대한 F−1 표본 분산을 영역 평균한 뒤 제곱근. 단일 영상의 공간 편차를 잡음으로 부르지 않음 |
| `mean_bias_dn`, `mean_abs_bias_dn` | 반복 평균과 독립 기준의 signed 평균 차이 및 pixel별 절댓값 차이의 평균 |
| `mean_baseline_delta_dn`, `mean_abs_baseline_delta_dn` | 같은 RAW의 v1 tone 이전 결과와의 차이. 독립 기준에 대한 bias와 구분 |
| `texture_amplitude_dn` | 명시한 period와 row/column 방향의 상수·sin·cos 성분을 맞춘 출력 진폭 |
| `texture_amplitude_ratio`, `texture_attenuation_fraction` | 독립 기준 진폭 대비 비율 및 `1 − ratio`. 음의 attenuation은 진폭 증폭 |
| `edge_overshoot_dn`, `edge_undershoot_dn` | 고정 방향·위치·폭의 edge band에서 독립 기준 양쪽 plateau를 넘거나 밑도는 양 |

Texture에는 `period_px > 2`와 방향이 필요합니다. Edge에는 방향·`position_px`·
`half_width_px`와 양쪽 plateau를 평가할 영역이 필요합니다. 기준 진폭이 0이거나
정해진 harmonic을 식별할 수 없는 영역도 `unknown`입니다.

각 metric은 `{value, unit, status, reason}`을 반환합니다. 알 수 없는 값은 `value: null`,
`status: "unknown"`과 이유를 보존합니다. 독립 기준이 없으면 bias·질감 비율·halo를
만들어 내지 않습니다. 대신 가능한 시간 잔차·출력 질감 진폭·baseline delta를 기록합니다.
전체 지표 목록과 정의는 `GET /api/v1/isp-studies/catalog`에 있습니다.

## 독립 기준과 데이터 분리

선택적인 독립 기준은 `camera_isp_reference_v1` metadata와 signed floating H×W×3
NPY입니다. 단위는 `normalized_linear_sensor_rgb`, 기준면은
`sensor_rgb_after_fixed_wb_ccm_before_tone`입니다. Geometry·pixel pitch·normalization과
공통 BLC/demosaic/WB/CCM이 해당 원본·기준 ISP와 일치해야 합니다. Tone 설정은 이
선형 기준에 포함하지 않습니다. 기준 NPY는 8 MiB 이하이며 각 영상 축은 512 이하입니다.

작성한 합성 기준은 `authored_independent`와 estimated/simulated 출처로 남깁니다.
`independent_measurement`에는 measured 출처, 실물 identity, 장비와 교정 revision이
필요합니다. 등록된 출처 선언만으로 실물 정확도나 기준의 독립성을 인증하지 않습니다.
기준의 불확실도는 이유를 포함한 `unknown`입니다. 후보 출력은 독립 기준이 될 수 없습니다.

Calibration fitting 자료와 Study 자료의 중복, 한 Study 안의 자료 재명명 중복을
취득 계보·RAW SHA·개별 frame SHA·취득 group으로 확인합니다. 독립 검증 자료는 선택의
부모 tuning 자료뿐 아니라 등록된 다른 과거 Study 자료와도 겹칠 수 없습니다.
이미 설정 선택에 사용한 자료를 새로운 이름으로 독립 검증에 넣을 수 없습니다.
이 검사는 보존한 계보와 선언에 대한 분리 검사이며, 기록 밖의 사용 이력이나 물리적
독립성을 보증하지 않습니다.

## 집계, 선택, 독립 검증

각 목적·제약은 metric과 명시한 dataset key 목록, `max`/`min`/`max_abs` reducer를
고정합니다. 목적에는 minimize/maximize 방향, 제약에는 `<=`/`>=`·값·단위를 적습니다.
예를 들어 잡음 상한과 질감 비율의 양쪽 허용 경계, halo 상한을 별도로 둘 수 있습니다.

Pareto는 필요한 결과가 모두 정의되고 제약을 만족한 **실행한 표본 후보 사이**에서만
계산합니다. 동률 후보는 유지합니다. 누락·실패·취소·unknown은 결과 범위와 잠정 상태에
남으며 보간·추정 점수로 채우지 않습니다. 전체 공간의 최적성, 통계적 우월성 또는
자동 winner를 주장하지 않습니다. 화면의 두 축 그림은 선택한 목적들의 투영이며,
목적이 더 많으면 서버의 Pareto 판정은 그 목적들을 모두 사용합니다.

Tuning 작업들이 종료되면 사용자가 후보 ID와 이유, 확인한 `analysis_sha256`으로 선택을
저장합니다. 선택한 후보의 모든 데이터셋 작업은 성공해야 합니다. 선택은 제약 충족이나
Pareto 여부를 대신하는 자동 승인 절차가 아니며, 저장된 분석과 함께 남는 공학적 판단입니다.
`analysis.status == "complete"`는 모든 작업이 종료되었다는 의미여서 실패·취소를
포함할 수 있습니다. 반드시 `coverage`와 각 행의 상태도 확인합니다.

독립 검증 계획은 `phase: "independent_validation"`, 저장한 `selection_id`, 그 선택의
`parent_study_id`, 같은 보정·기준 ISP를 사용합니다. 탐색 축은 비우고 새로운 데이터셋의
purpose도 `independent_validation`으로 지정합니다. 기준 후보와 선택한 프로파일만
실행하며 검증 결과로 새 순위를 만들지 않습니다(`not_ranked_validation`).

## 화면과 API·SDK

실험 화면의 **같은 RAW의 ISP 탐색**에서 원본·보정·기준 ISP를 선택합니다. 해결할 질문,
데이터셋 역할·정지 선언·ROI·독립 기준, 축과 지표 집계를 작성하고 전체 계획을 검사합니다.
계획의 작업 수·자원·고정 입력을 확인해 제출한 뒤 후보×데이터셋 진행과 프레임별 단계
비교, unknown 이유를 봅니다. 선택과 별도 검증을 저장하고 사례로 연결할 수 있습니다.

| API | 용도 |
|---|---|
| `GET /api/v1/isp-studies/catalog` | 지원 축·한계·지표 정의 |
| `GET /api/v1/templates/isp-study` | source_kind/source_id/calibration_id/baseline_isp_model_id로 계획 형식 생성 |
| `POST /api/v1/isp-studies/plan` | specification 검사·정규화·고정 manifest와 plan_hash 반환; 등록 없음 |
| `POST /api/v1/isp-studies` | `{specification, plan_hash}`로 묶음 등록 |
| `GET /api/v1/isp-studies/{id}` / `.../{id}/analysis` | 보존 Study·현재 작업별 분석 조회 |
| `GET /api/v1/isp-study-cells/{id}` | 전용 작업의 원본·단계 산출물 조회 |
| `POST /api/v1/isp-studies/{id}/cancel` | 해당 Study가 소유한 미완료 작업 취소 |
| `POST /api/v1/isp-studies/{id}/selection` | 후보·이유·분석 해시를 고정한 선택 |
| `POST /api/v1/isp-references/import` | metadata JSON과 image NPY의 multipart 기준 등록 |
| `GET /api/v1/isp-studies/{id}/export` / `.../{id}/bundle` | 근거 manifest JSON / artifact 포함 ZIP |

SDK도 같은 API를 사용합니다. 아래 ID와 `base_url`은 해당 개발 서버에서 확인한 값으로
지정합니다. 기본 설치본 주소에 dev6 API가 있다고 가정하지 않습니다.

```python
from pathlib import Path
from camera_platform.sdk import PlatformClient

with PlatformClient(base_url) as client:
    specification = client.isp_study_template(
        "run", source_id, calibration_id, baseline_isp_model_id
    )
    # 제출 전에 질문, 반복 자료, ROI, 축, 목적과 제약을 검토·수정한다.
    plan = client.plan_isp_study(specification)
    study = client.submit_isp_study(plan)
    analysis = client.wait_isp_study(study["id"])
    print(analysis["coverage"], analysis["aggregate"])
    Path("isp-study-evidence.zip").write_bytes(client.isp_study_bundle(study["id"]))
```

검토한 후보를 선택할 때는 `select_isp_candidate(study_id, candidate_id, rationale,
analysis_sha256)`를 호출합니다. 응답이 불확실한 제출은 저장한 같은 specification과
plan_hash로 재시도합니다. 실패·취소 뒤 새 실행이 필요하면 이전 Study를 부모로 보존하고
attempt를 증가시킨 새 계획을 만듭니다. 다른 Study의 작업을 공유하지 않아 취소 소유권이
섞이지 않습니다. 완료한 결과는 취소 요청으로 지우지 않습니다.

## 사례 근거와 재현

사례에는 `kind: "isp_studies"` 근거를 추가하고 `dataset_requirements`로 각 dataset key를
해당 요구사항 key에 연결합니다. 그 합집합은 근거의 `requirement_keys`와 같아야 합니다.
각 연결은 **그 데이터셋의 실제 원본 Test**와 요구사항의 Test가 같아야 하며, 원본 assembly도
사례에 포함되어야 합니다. 서로 다른 시험의 장면을 하나의 요구사항에 일괄 연결하지 않습니다.

Run의 보존 Test snapshot·실제 조건, capture의 원래 관측 계약·Test 결속을 다시 확인합니다.
예를 들어 등록한 Test와 다른 노출로 override한 Run은 그 원래 Test의 사례 근거가 될 수
없습니다. 모든 cell이 종료된 뒤 결과·실패·unknown을 포함해 근거를 고정합니다. 선택 기록은
해당 Study 또는 독립 검증의 부모 선택에 결속되어야 합니다. 사례의 판단·남은 문제·다음
시험은 별도 사용자 결정으로 보존하며 Study 성공만으로 승인하지 않습니다.

계획은 RAW bytes와 frame 순서, geometry·조건, 보정·기준·후보 프로파일·독립 기준,
지표 버전·집계, 모델/Factory/보정 계보와 구현·환경 hash를 고정합니다. Worker와 캐시 조회도
고정 snapshot과 artifact 해시를 대조합니다. 후보 모델은 기준의 부모 관계를 보존한
estimated 설정이며 이전 모델의 실물 identity를 상속해 실측 후보처럼 표시하지 않습니다.

근거 ZIP에는 manifest와 보존 입력·단계 배열·validity mask·지표·미리보기 등이 artifact
SHA별로 포함됩니다. 고정 평가 mask와 v1 tone 이전 baseline도 남습니다. 이 ZIP은
검증할 수 있는 계산 근거이며, 외부 solver·장비까지 포함한 이식 가능한 실행 환경이나
실물 승인서를 의미하지 않습니다.

## 플랫폼 안의 위치와 남은 범위

원문의 일곱 모듈은 유지합니다. Asset Registry가 원본과 계보를 보존하고, Model Factory와
Camera Composer의 모델·보정·적용 조건 계약을 재사용합니다. Simulation Runtime은
고정 RAW 재처리를, Experiment & Optimization은 유한 후보 비교를 수행합니다.
Validation Center의 조건·독립성 원칙과 사례 판단을 연결하고, Agent & Release Control은
동일 API와 근거·감사 흐름을 사용합니다.

네 실행 방식인 **Quick Estimate·Response-DB·Surrogate·Detailed Characterization**도
유지합니다. 이 기능은 Response-DB의 보존 RAW 활용 흐름 안의 제한된 ISP 탐색이며,
화면에 실험 항목이 추가된 것을 새로운 물리 실행 방식으로 세지 않습니다.

현재 범위 밖에는 보정 fitting, 시간 denoise, 임의 ISP graph 탐색, 적응형·전역 최적화,
제조사 HW ISP 동등성, 일반 광학·픽셀·전기 모델의 실물 정확도와 총불확실도 평가가
남아 있습니다. 합성 RAW의 계산 일치와 출처 무결성은 실물 검증을 대신하지 않으며,
독립 실측 성능·총불확실도와 전체 상용화 Goal의 완료는 주장하지 않습니다.

실제 검증에서는 보존 근거와 반복되는 계보의 크기, 엄격한 재검증에 따른 조회 지연도
관측했습니다. 현재 작업 수 제한 안에서 실행되는 것과 제품 사용량을 늘려도 빠르게
조회할 수 있는 것은 별도 조건입니다. **사용량 확대 전에 근거 중복과 조회 비용을
개선해야 합니다.** 원본 해시·계보 확인을 유지하면서 저장 표현과 반복 검사 비용을
줄이는 작업이 남아 있습니다.

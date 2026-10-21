# 다음 최소 구현: 동일 RAW의 ISP 후보 비교를 사례 판단에 연결

2026-09-21, 소스 `v0.16.0.dev5`를 확인해 작성한 **미구현 제안**입니다.
이 문서의 ISP Study 계약·API·Worker 작업·사례 연결은 아직 제공 기능이 아닙니다.
이번 작업은 문서만 추가하며, 설치본 전환·실측 검증·자동 최적화 완료를 뜻하지 않습니다.

첫 질문은 **“고정된 native RAW와 보정에서 잡음 감소를 얻으면서 어느 정도의 질감 손실과
edge halo를 허용할 것인가?”**입니다. 사용자가 제약과 관찰 결과를 보고 후보를 선택하고,
다른 취득 자료에서 그 선택을 확인해 사례의 판단과 다음 시험으로 남기는 데 범위를 둡니다.

## 현재 구현과 이번 제안의 차이

| 확인한 현재 범위 | 근거와 후속 경계 |
|---|---|
| dev5 순수 ISP v2 | [`isp_v2.py`](../src/camera_platform/isp_v2.py)의 native 단일-stream RAW, LSC·same-site denoise·bounded sharpen과 v1 baseline. 입력 RAW를 새로 생성하지 않습니다. |
| dev5 단일 replay의 제품 연결 | [`isp_replay_v2.py`](../src/camera_platform/isp_replay_v2.py), API·SDK·편집 UI가 보정과 프로파일을 고정해 한 원본을 재처리합니다. 현재 지원 계약은 [고급 ISP 문서](camera-platform-advanced-isp.md)를 따르며, 배포·재시작 검증은 별도 실행 근거로 확인합니다. |
| 독립 도구의 후보 비교 | [`verify_camera_isp_tradeoffs.py`](../tools/verify_camera_isp_tradeoffs.py)가 합성 flat/edge/texture 각 16×48×48 RAW에 6개 설정을 적용했습니다. 총 18개 scene/설정 결과, 136개 검사 및 지표 시험 4개가 통과했습니다. **API Study·Worker 후보 탐색·사례 근거로 등록한 실행은 아닙니다.** |
| 기존 설계 Study | [`study_contracts.py`](../src/camera_platform/study_contracts.py), [`studies.py`](../src/camera_platform/studies.py)는 Quick Estimate/Response-DB에서 노출·QE·전기 변수를 바꾸어 새 계산을 만드는 계약입니다. 고정 RAW의 ISP 탐색은 지원하지 않습니다. |
| 기존 사례 | [`case_contracts.py`](../src/camera_platform/case_contracts.py)의 근거 종류는 runs/measurements/validations/studies입니다. [`case_reviews.py`](../src/camera_platform/case_reviews.py)는 카메라 Run만 허용하고, 기존 Study의 기준 test 하나와 후보 Run들을 검사합니다. replay를 runs로 가장해 넣을 수 없습니다. |

도구의 [비교 그림](../reports/camera_platform/advanced-isp-v016/tradeoffs/isp-tradeoffs.png)과
[근거 JSON](../reports/camera_platform/advanced-isp-v016/tradeoffs/tradeoff-evidence.json)은
입력·보정·프로파일·구현·환경 해시 및 실제 단계 배열을 보존합니다. Denoise 0→0.8,
sharpen 0의 합성 flat temporal residual은 5.435→3.954 DN으로 줄었지만 texture 진폭 비율은
1.012→0.901이었습니다. Sharpen 0.6의 edge overshoot는 약 18.6 DN입니다.
이 수치는 서로 다른 효과와 손실을 보여주는 유한 표본 결과이며, 하드웨어 SNR·광학 MTF·
실물 성능 보증이나 선택된 최적값이 아닙니다. 이 데이터는 calibration fitting에 쓰지
않았지만 ISP 설정 비교에는 이미 사용했으므로, 선택 후의 독립 검증 세트라고 부르지 않습니다.

## 기존 구조에서 그대로 재사용하면 안 되는 부분

1. `StudyPlan.plugin_id`와 parameter/metric catalog는 광전자·노출 계산에 한정됩니다.
   여기에 ISP 값만 추가해 기존 `metrics(run)`을 사용하면 실제 영상 지표가 아닌
   이론 SNR 또는 새 취득 결과를 같은 RAW 비교처럼 보여줄 수 있습니다.
2. 기존 `Registry.publish_study`는 전체 계획을 원자적으로 공개하고 24개 Run 상한을
   지키지만, 기존 카메라 요청 및 `run_ids` 구조입니다. 후보×dataset 전체 작업과
   ISP 모델·metric 정의를 고정하는 전용 batch 계약이 필요합니다.
3. 사례의 `_study`는 단일 기준 test와 카메라 Run 계보를 확인합니다. 여러 장면의
   replay·평가 지표에는 별도 resolver가 필요하며 기존 무결성 검사를 느슨하게 하면 안 됩니다.
4. [`workbench.py`](../src/camera_platform/workbench.py)의 요약은 근거 원문을 대체하지
   않습니다. 새 Study를 추가하면 collection·정확한 detail hash·Worker 결과 조회 경로도
   함께 연결해야 합니다. `studies.js`의 SNR 고정 표만 재사용해서는 새 지표가 설명되지 않습니다.

## 최소 입력과 고정 조건

제안 schema는 `camera_isp_study_v1`, 별도 collection은 `isp_studies`입니다. 기존
`camera_parameter_study_v1`의 의미와 재현 해시는 유지합니다. 최초 구현은 다음만 받습니다.

- 이미 Registry가 보존한 단일-stream integer native RAW source와 원래 acquisition
  assembly/test, format·full shape·crop origin·CFA phase, frame 순서·취득 조건.
- 모든 선택 source에 적용 가능한 **하나의 불변 calibration ID/전체 기록 hash/numeric
  blob SHA**. 다른 조건으로의 재사용은 기존 `resolve_for_source` 검사를 모두 통과해야 합니다.
- 하나의 baseline v2 profile과 허용한 두 축 `raw_denoise.strength`,
  `linear_sharpen.amount`의 명시적 값 목록. LSC 설정, noise floor, sharpen threshold/
  max_delta, BLC·WB·CCM·tone은 고정합니다. Calibration이나 취득 설정을 후보 축으로 바꾸지 않습니다.
- dataset별 역할(flat/edge/texture), 반복 취득 group ID, ROI/halo, reference artifact,
  기준 처리 단계·단위, source kind, tuning 또는 independent_validation의 용도.
- 사례 질문, 후보의 목적 지표와 단위 있는 제약, 모든 metric/aggregation 버전.

최초 예산 제안은 기준 후보를 포함해 **후보×dataset 작업 24개 이하**입니다. 같은
정규화 profile은 중복하지 않고 기준 후보를 항상 보존합니다. v1 baseline은 각 처리에
이미 포함되므로 별도 후보 작업으로 중복 생성하지 않되, 연산·출력 예산에는 포함합니다.
반복 잡음 평가용 dataset은 동일 조건의 정지 장면 4–16 frame으로 제한하고 16 frame을
기본 예제로 사용합니다. 각 replay의 기존 array/frame-pixel 예산도 그대로 검사합니다.
총 저장 배열·preview·reference 크기와 queue 점유까지 계획에 표시하며 자동 resize하지 않습니다.
탐색 축에 해당하는 stage mode는 처리 가능한 고정 mode여야 합니다. Bypass에 가려 값이
소비되지 않는 축은 preflight에서 거부하고, strength/amount 0의 identity는 명시적으로 보존합니다.

한 source의 모든 후보는 **동일한 NPY blob과 동일한 frame 순서**를 읽습니다. RAW byte
SHA를 제출 전·Worker·결과에서 대조하며 원본 readout·노출·gain·black·white·crop을 바꾸지
않습니다. 서로 다른 장면의 RAW가 같은 배열일 필요는 없지만, 같은 장면의 후보 간 입력은
완전히 같아야 합니다. HDR float 결과, paired DCG를 frame으로 펼친 입력, 대표 raster,
TNR·AE/AWB·새 촬영은 이 slice에서 지원하지 않습니다.

## Calibration, tuning, 독립 검증의 분리

Calibration 원본/fit group, 설정을 비교하는 tuning group, 선택 후 확인하는 validation
group을 manifest에서 분리합니다. 바이트 SHA 중복뿐 아니라 원래 capture/scene/acquisition
group ID를 검사해 같은 촬영의 frame 분할을 독립 취득으로 바꾸지 않습니다. 실측 자료의
독립성이 확인되지 않으면 상태를 `unknown`으로 남깁니다. 서로 다른 파일명이나 seed만으로
물리적 독립성을 인증하지 않습니다.

첫 합성 예제는 calibration 계수를 평가 RAW 생성 전에 명시하고 fit하지 않습니다.
정지 flat, edge, texture의 tuning 반복들을 같은 방식으로 처리합니다. Calibration이
적용 가능한 범위 안에서 별도 seed와 별도 authored scene/acquisition group의 검증 자료를
미리 고정합니다. 서로 다른 장면·밝기·공간 위치의 coverage는 각각 명시하며 일반화를 과장하지 않습니다.

1. Tuning Study에서 모든 후보와 손실·제약을 확인합니다.
2. 사용자가 후보 ID/정확한 profile hash, 선택 이유, tuning Study와 결과 snapshot hash를
   고정한 selection 기록을 만듭니다. 자동 winner나 프로파일 승격은 없습니다.
3. 별도 validation Study는 이 selection과 calibration을 고정하고 선택 후보·기준만
   처리합니다. Validation 결과는 tuning Pareto와 섞지 않습니다.
4. Validation을 본 뒤 설정을 바꾸면 새 tuning revision이며, 같은 자료를 계속
   independent validation으로 표시하지 않습니다. 새 검증 자료 또는 미확립 상태가 필요합니다.

## 비교 지표와 제외 규칙

최초 비교는 tone 전 `sharpened_rgb_frames`와 해당 baseline을 사용합니다. 표시 PNG의
gamma·clip·자동 정규화로 수치를 계산하지 않습니다. 산술 sensor-RGB guide의 normalized
값에 명시한 `white-black`을 곱한 `DN_equivalent`를 사용하며 휘도·전자·광량으로 부르지 않습니다.
R/Gr/Gb/B 단계 배열과 RGB 배열은 별도 축으로 보존합니다.

| 축 | 첫 지표·고정 기준 | 해석과 제약 |
|---|---|---|
| 반복 잡음 | 고정 ROI의 각 pixel에서 temporal mean을 뺀 잔차에 표본 분모 F−1을 적용한 RMS | 단일 frame 공간 표준편차를 잡음으로 사용하지 않습니다. 움직임·노출 변화·반복 조건 불일치는 잡음 지표 지원 밖입니다. |
| Bias | 반복 평균 출력과 독립 reference의 signed 평균 차이, 추가로 절댓값 | Reference 없는 실측 RAW의 v1 대비 변화는 bias가 아닌 `baseline_delta`로 별도 표시합니다. |
| Texture | 미리 선언한 주파수·ROI의 sine/cosine amplitude와 독립 reference amplitude의 비율 | 진폭 감쇠와 boost를 모두 표시합니다. 최대화만 하면 oversharpen을 보상하므로 허용 하한·상한 또는 1과의 거리로 제약합니다. |
| Halo | 고정 edge 방향/위치에서 반복·행 평균한 profile의 기준 high/low 대비 overshoot/undershoot | 두 값을 분리하며 다른 edge contrast를 섞어 평균하지 않습니다. 처리 영상 halo이며 Lens MTF가 아닙니다. |

Reference는 독립 authored synthetic truth 또는 출처·취득 방법·불확실도가 기록된 고정
reference asset입니다. 후보 출력에서 reference를 만들거나 best candidate를 truth로 쓰지
않습니다. v1 baseline은 변화 비교의 기준일 수 있으나 자동으로 물리 truth가 되지 않습니다.
Reference amplitude 0, 불충분 반복, 잘못된 ROI, invalid stencil은 null/사유로 남깁니다.

평가 ROI와 mask는 후보 결과를 보기 전에 정합니다. 전체 후보의 최대 지원 stencil을
기준으로 halo를 제외하고, 고정 source/calibration domain과 mask 생성 버전을 저장합니다.
후보별로 유리한 pixel만 다시 선택하지 않습니다. 고정 ROI의 필수 pixel이 invalid이면
해당 metric/제약은 unknown이며 invalid 비율과 이유를 함께 보존합니다. 여러 후보의
출력 mask 교집합으로 사후 ROI를 축소해 실패를 숨기지 않습니다.

모든 dataset 행을 보존합니다. 서로 다른 장면·노출·contrast를 몰래 pooling하지 않습니다.
Pareto에 쓸 집계가 필요하면 `max_over_named_datasets` 등 reducer와 대상 dataset을 계획에
고정합니다. Signed bias의 양·음 상쇄를 허용하지 않는 `max_abs_bias`, overshoot/undershoot의
각 상한, texture ratio의 양쪽 bound 등을 별도 제약으로 둘 수 있습니다. 종합 점수는 없습니다.

제약 충족/위반/unknown과 목적 값 정의 여부를 먼저 판단한 뒤, 같은 tuning 자료와
고정 지표를 모두 가진 후보 사이에서만 sampled Pareto를 계산합니다. 실패·취소·누락이
있으면 잠정 결과이며, 동률은 함께 남깁니다. Validation 후보끼리 새 순위를 만드는 것과
전체 parameter 공간의 최적해, 통계적으로 유의한 우열, 총불확실도 보장은 포함하지 않습니다.

## 불변 계획, API와 Worker의 수직 구현

다음 route와 이름은 **새로 구현할 제안**입니다. 현재 호출 가능한 endpoint 목록이 아닙니다.

| 제안 API | 역할 |
|---|---|
| `POST /api/v1/isp-studies/plan` | 전체 profile×dataset preflight, 정규화 manifest와 plan_hash·자원 반환. 파일/DB/queue 변경 없음. |
| `POST /api/v1/isp-studies` | 같은 명세·plan_hash·원본·환경을 재검사해 후보 모델과 전체 작업을 원자적으로 공개. |
| `GET /api/v1/isp-studies/{id}/analysis` | 정확한 작업별 metrics, 제외 이유, coverage·제약·잠정 Pareto 반환. |
| `POST /api/v1/isp-studies/{id}/cancel` | Study 소유 미완료 작업 취소 요청과 개별 처리 상태 기록. |
| `GET /api/v1/isp-studies/{id}/bundle` | manifest, source/reference/calibration/profile, 결과·배열·지표 근거·환경의 독립 검증 가능한 묶음. |

Manifest는 다음을 canonical JSON으로 고정합니다.

- schema·질문·phase·parent Study/selection, 모든 source의 전체 snapshot hash와 RAW blob
  SHA, byte 수·dtype·shape·axis·frame order·geometry·original acquisition 조건.
- Calibration 전체 기록/원본·정규화 blob SHA 및 fit/source lineage, baseline profile,
  정규화 후보 profile/모델 parent·source·content hash. 편집한 ISP 후보는 `estimated`,
  authored tuning이며 기존 measured label/physical instance를 자동 승계하지 않습니다.
- Dataset group/split/reference·ROI·mask·stage/unit, metric/reducer 정의와 구현 버전,
  axes·constraints·objectives, 전체 후보×dataset index와 예상 출력량.
- 제품 source fingerprint, 순수 ISP·calibration·metric 구현 SHA, Python/dependency
  버전과 환경 hash. 작업 요청 hash와 결과/artifact를 포함한 전체 기록 hash는 구별합니다.

최소 Worker 단위는 **한 후보×한 dataset**입니다. 새 wrapper가 기존 v2 replay의
원본·보정 검증과 처리·저장 helper를 재사용하고, 같은 작업 안에서 보존 float 배열로
metric을 계산해 함께 저장합니다. 브라우저나 요청 handler에서 장시간 수치 처리를 하지
않습니다. 기존 replay의 job_kind를 바꿔 snapshot 대조를 우회하지 않고, 공통 helper를
분리해 두 caller가 동일한 frozen replay 입력 검사를 사용하도록 구현합니다.

Worker는 실행 직전 전체 원본·모델·보정·reference·환경을 다시 검사하고, RAW byte
불변·actual stage 적용/mask를 결과에 기록합니다. 지표도 metric/version·ROI·사용 frame·
reference SHA 및 계산한 배열 artifact SHA를 가집니다. Pareto 집계는 저장된 metric만
읽는 가벼운 서버 분석이며, 별도 새 시뮬레이션이나 mutable profile을 소비하지 않습니다.

Registry batch publication은 기존 Study의 transaction 방식을 재사용하되 전용 계약으로
추가합니다. 전체 queue 용량·모델/작업/Study 기록을 함께 검사하고 실패하면 부분 공개가
없어야 합니다. 같은 plan_hash 제출은 같은 Study를 반환합니다. 응답이 불확실한 POST는
보존한 plan_hash/request identity로 조회·재제출하며 새 계획을 무조건 만들지 않습니다.

최초 cache 범위는 **동일 Study 재제출/재조회**로 한정합니다. 서로 다른 Study 사이의
작업 공유는 취소 소유권을 복잡하게 하므로 첫 구현에서 하지 않습니다. 성공 cache라도
원본·보정·reference와 결과 blob hash/size를 다시 검사하며 누락·변조를 성공으로 반환하지
않습니다. 실패·취소 작업은 결과 cache로 쓰지 않고, 재실행은 이전 Study를 참조한 새
명시적 attempt로 보존합니다. 구현/환경 변경 뒤 예전 계획을 조용히 계속 실행하지 않습니다.

취소는 완료 결과를 지우지 않습니다. queued/running에만 적용하고 기존 Worker child
process 종료·terminal 상태 보호를 재사용합니다. 취소 요청과 실제 terminal coverage를
구별하고, 마지막 완료와 취소의 race에서도 이미 성공한 결과를 취소로 덮지 않습니다.
실패·취소·미완료 후보가 포함된 분석을 전체 성공으로 표시하지 않습니다.

## UI와 사례·근거·판단 연결

실험 화면에 **동일 RAW의 ISP 후보 비교** 모드를 추가하고 기존 새 촬영 Study와 분리합니다.
원본 dataset·보정·고정 공통 설정을 먼저 선택한 뒤 두 축의 값과 제약을 작성합니다.
전체 계획에서 후보 수뿐 아니라 dataset×작업 수, 기준, source hashes와 예산을 확인하고
제출합니다. 진행/실패/취소도 후보×dataset 격자로 보여줍니다.

결과는 noise/bias/texture/halo별 그래프와 제약 표, source/frame을 고정한 v1/v2 영상,
RAW·단계 배열·mask drill-down을 제공합니다. 축·ROI·display range를 바꿔 결과 의미를
감추지 않습니다. 사용자가 고른 두 목적의 Pareto는 나머지 제약·dataset coverage를 함께
보여줍니다. 설정 또는 source 변경 시 draft plan을 무효화하고, 늦은 preview/analysis
응답은 study ID·plan hash·render epoch를 확인해 현재 초안을 덮지 않게 합니다.

사례에는 새로운 `isp_studies` evidence kind와 전용 resolver가 필요합니다. 여러 scene의
고정 test를 하나의 다른 test로 대체하지 않고, **dataset별 requirement key→원래 test
revision**의 명시적 대응을 추가합니다. 연결하는 source의 모든 acquisition assembly가
사례에 있어야 합니다. ISP 후보를 취득 assembly에 끼워 넣어 Camera v2 지원을 가장하지 않습니다.

서버는 terminal Study의 전체 manifest·모든 작업 상태/결과·원본/보정/reference/profile
artifacts와 analysis snapshot hash를 고정합니다. Requirement에 연결한 dataset scope도
보존하고, 요청/결과 hash 또는 artifact 무결성이 달라지면 사례 조회를 거부합니다. 기존의
‘근거 확인 불가’ 목록 격리 동작과 append-only 리뷰 이력은 유지합니다.

사용자는 `candidate`, `investigate`, `rejected`, `deferred` 등의 판단과 이유, 선택 profile,
남은 제약 실패·독립성 unknown, 다음 validation/실측 계획을 기록합니다. Evidence를
연결하는 행위가 판단을 자동 변경하지 않으며, selection이나 Pareto는 실물 검증·release
승인 권한을 만들지 않습니다. Validation을 마쳐도 어떤 장면·조건에서 확인했는지 남깁니다.

## 최소 완료 기준과 구현 순서

1. **계약·수치 경계:** fixed source/calibration/profile manifest, metric 및 dataset split
   검증부터 작성합니다. 반복 분산의 독립 scalar, bias/halo/harmonic truth, fixed-mask
   거부, missing reference/zero amplitude·미정 제약/Pareto 동률 시험이 필요합니다.
2. **API·Registry·Worker:** 전 후보 preflight 무변경, batch 원자성·재제출, 원본 불변,
   환경 변경/원본 변조/잘못된 calibration 거부, 실제 단계 pixel 변화와 byte-identical
   bypass를 별도 API와 Worker에서 확인합니다. 취소·강제 종료·재시작·cache 변조도 포함합니다.
3. **제품 결과 UI:** 3개 합성 장면×소규모 후보에서 화면 수치와 보존 배열을 대조합니다.
   오류·부분 완료·늦은 응답·상세 재열기·JSON/ZIP의 원본 대응을 확인합니다.
4. **사례와 독립 검증:** 요구사항/test/dataset 연결, terminal 전체 근거, 선택 기록,
   별도 검증 Study와 새 사례 리뷰까지 한 흐름을 실제 UI/API/Worker로 수행합니다.
   원래 RAW/profile/calibration 및 이전 판단은 변경되지 않아야 합니다.

이를 통과해야 ‘ISP 후보 비교가 제품의 실험·사례 판단에 연결됐다’고 보고합니다.
현재 도구의 18개 결과를 이 완료 근거로 승계하지 않습니다. Calibration fitting,
자동 최적 후보 선택, adaptive search/surrogate, 실물 holdout 성능, 제조사 HW ISP 동등성,
TNR·HDR/DCG 전용 graph, 전체 불확실도와 상용 출시 gate는 후속 범위로 남습니다.

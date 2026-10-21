# 설계 변수 탐색 · 후보 비교

실험 화면의 **설계 변수 탐색**은 설계 질문, 기준 Camera/Test, 변경할 변수, 비교 목표와
수치 제약을 하나의 불변 계획으로 연결합니다. API와 Python SDK에서도 같은 경로를
사용합니다. 현재는 Quick Estimate와 평탄장 Response-DB의 유한한 설계 탐색입니다.
원문 전체의 일반 최적화·공차·Surrogate·적응형 실험·Agent가 완료된 것은 아닙니다.

## 입력과 범위

| 변수 | 값의 의미 | 단위 |
| --- | --- | --- |
| `exposure_s` | 새 시험의 노출 | s |
| `irradiance_scale` | 등록된 센서면 분광 광량 전체에 곱할 배율 | 1 |
| `qe_scale` | 등록된 수집전자 QE 전체에 곱할 배율 | 1 |
| `read_noise_e` | 새 Sensor 응답의 Read noise | electron |
| `dark_current_e_per_s` | 새 Sensor 응답의 Dark current | electron/s |
| `full_well_e` | 새 Sensor 응답의 Full well | electron |
| `gain` | Sensor readout와 시험·Camera 조건에 함께 적용할 Gain | 1 |

`one_at_a_time`은 다른 변수를 원본 기준값에 고정합니다. `cartesian`은 명시된 값의
모든 조합을 실행합니다. 둘 다 원본 기준점을 보존하고 같은 점은 중복 실행하지 않습니다.
여러 Camera 조합에 적용할 수 있으며 **전체 기준점 포함 24개 Run**이 상한입니다.
배열 배율의 기준은 1입니다. QE·노출·Readout의 물리 범위와 모든 모델의 유효 영역을
검사하며 영역을 자동으로 넓히지 않습니다. 기본 예제의 Gain 영역은 1로 고정돼 있습니다.

F-number·온도·물체 거리 같은 Metadata만 바꾸어 광학/전기 응답을 계산한 것처럼
표시하지 않습니다. 현재 계산에서 실제 소비하는 위 변수만 지원합니다. 공간 PSF·시간
장면·일반 ISP Graph와 RAW 재처리는 별도 계약이며 아직 이 탐색기에 연결되지 않았습니다.
실측 RAW 관측 시험은 취득 조건을 고정하므로 설계 탐색 입력으로 사용하지 않습니다.

## 원본·실행·불확실도

1. `POST /api/v1/studies/plan`: 메모리에서 모든 후보의 모델·시험·Camera를 구성하고
   실제 Runtime 사전 검사를 수행합니다. DB·작업 큐·파일에 쓰지 않습니다. 계획에는
   입력 값, 원본 ID/내용 해시, 환경, 후보별 자원 배열 추정과 `plan_hash`가 있습니다.
2. `POST /api/v1/studies`: 검사한 명세와 계획 해시를 다시 대조합니다. 새 버전, 전체
   작업, Study를 하나의 SQLite Transaction으로 공개합니다. 용량 부족·저장 실패는
   부분 모델이나 작업을 남기지 않습니다. 같은 계획의 재제출은 기존 Study를 반환합니다.
3. Worker가 각 Run을 실행합니다. 변경한 모델은 `estimated`, 새 revision과 원본
   parent를 갖습니다. 원본 모델과 실측은 바뀌지 않습니다. Gain 변경은 Model/Test/Assembly에
   일관되게 적용합니다. 기존 Calibration 계보와 적용 범위를 유지합니다.
4. 바뀐 입력에 원래의 절대 불확실도 경계가 있으면 새 후보에서는 `unknown`으로
   바꿉니다. 기존 수치 경계를 다른 Nominal에 자동 이동하지 않습니다. 바꾸지 않은 입력
   경계도 조건부이며, 전체 실물 예측 불확실도를 해결하지 않습니다.

실행 자원 추정은 배열 크기이며 실측 Peak memory, 소요 시간, 요금 견적이 아닙니다.
Study 제출 자체는 최종 제품 승인이나 배포가 아닙니다.

## 무엇을 비교하는가

- **최저 이론 SNR**: 모든 모델 상고·채널의 비포화 Poisson/Read-noise SNR 최솟값.
  하나라도 정의되지 않으면 unknown입니다. 실제 센서의 EMVA 측정 결과가 아닙니다.
- **최소 포화 여유**: 유효 Readout 용량에서 기대 광전자와 Dark 전자를 뺀 최솟값.
- **평균 기대 광전자**: 모델 상고·채널 표본의 산술 평균. 영상 면적 가중 평균이 아닙니다.
- **최대 상대 색 비율 변화**: 각 상고의 R/G·B/G를 자기 On-axis 비율로 나눈 변화량의
  절댓값 중 최댓값. Green/기준 비율이 정의되지 않으면 unknown이며 색채 정확도와 다릅니다.
- **노출 시간**: 시험에 선언된 ms. 움직임·Frame-rate 비용을 계산한 것은 아닙니다.

목표마다 최소화/최대화 방향을 명시합니다. 제약은 단위가 있는 `<=`/`>=`입니다.
실패·취소·미완료 Run, 미정 Metric과 제약 미충족은 비교 대상에서 제외하며 사유를 남깁니다.
전체 결과가 모이기 전이나 실패한 후보가 있으면 Pareto 비교는 잠정적입니다.

비지배 후보는 **계산한 유효 표본 중 모든 목적에서 같거나 더 좋고 하나 이상에서
엄격히 더 좋은 다른 후보가 없는 것**입니다. 가중 합산 점수, 물리 검증, 전체 공간의
최적해 또는 불확실도에 강건한 Pareto 전선을 의미하지 않습니다. 목적 방향과 Pareto
비교의 개념은 [Optuna 다목적 예제](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/002_multi_objective.html)를 참고했습니다. 현재 구현은 결정론적 표본 비교이며 Optuna 실행을 주장하지 않습니다.

민감도 표는 다른 축을 기준값에 고정한 인접 두 점의 `ΔMetric/ΔParameter`입니다.
구간·단위·양쪽 Run ID를 보존합니다. 여러 Step-size를 반복해 수렴한 미분이나 전역
민감도 분석은 아니며 미분 오차는 unknown입니다. 유한차분에서 Step-size와 수렴
확인이 별개라는 점은 [SciPy 미분 문서](https://docs.scipy.org/doc/scipy/reference/generated/scipy.differentiate.derivative.html)를 참고했습니다. `cartesian`에서도 다른 축이 기준값인 점이 있을 때만 이 변화율을 제공합니다.

## 사용 경로와 근거

```python
from camera_platform.sdk import PlatformClient

with PlatformClient() as platform:
    info = platform.get("bootstrap")
    plan = platform.plan_study({
        "name": "노출과 Read noise", "question": "짧은 노출에서 필요한 SNR을 확보할 수 있는가?",
        "assembly_ids": [info["demo"]["assembly_ids"][0]],
        "test_id": info["demo"]["test_id"],
        "axes": [
            {"parameter": "exposure_s", "unit": "s", "values": [0.002, 0.008]},
            {"parameter": "read_noise_e", "unit": "electron", "values": [1.0, 5.0]},
        ],
        "objectives": [
            {"metric": "exposure_ms", "direction": "minimize"},
            {"metric": "min_theoretical_snr_db", "direction": "maximize"},
        ],
    })
    study = platform.submit_study(plan)
    analysis = platform.wait_study(study["id"])
```

화면은 현재 분석 JSON을 내보내며 원본 Study/Run/Source 해시·환경·Metric 정의를 포함합니다.
RAW 배열까지 포함한 Study 전체 실행 환경 패키지는 아닙니다. 각 후보의 계산 화면에서
Run 원본과 Artifact를 확인합니다. 저장된 Study로 다시 열어도 같은 명세와 Run이 연결됩니다.

- [계약·동작 시험](../tests/platform/test_studies.py): 해석적 노출 배율, 독립 변수 변화,
  실제 RAW 변화, 한정 유효 범위, 원본/경계 보존, 계획 원자성·재제출, 수작업 Pareto 기준.
- [실제 브라우저/Worker](../reports/camera_platform/studies-ui/headless-qa.json): 계획 무변경,
  다섯 실제 계산, 제약 제외, 표시 수치·변화율·JSON, 후보 Run 이동과 Desktop/Mobile.

현재 및 설치 검증의 정확한 소스 버전은 각 보고서에 기록합니다. 남은 목표는 일반
Lens/Pixel/ISP/Scene 파라미터, 수치·물리 불확실도를 포함한 공차/민감도, 적응형 샘플링,
Surrogate Holdout/범위 검증, 다목적 탐색 알고리즘 및 Agent 실험 정책입니다.
